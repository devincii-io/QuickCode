import test from "node:test";
import assert from "node:assert/strict";
import {
  LANES, LANE_AGENTS, LANE_TOOLS, configTarget, laneOf, previewOf, roleOf,
} from "../../quickcode/frontend/js/trajectory/roles.js";
import {
  GAP_MIN_MS, buildSegments, clampView, eventTimes, homeSegment, isFitted, pickStep, planTicks,
  revealView, toReal, toVirt, zoomView,
} from "../../quickcode/frontend/js/trajectory/timeaxis.js";
import {
  INFERRED_CAP_MS, MAX_TRACKS, advanceLive, buildModel,
} from "../../quickcode/frontend/js/trajectory/model.js";
import {
  MIN_BAR_PX, bottomTop, cullBars, hitTest, laneTrackAt, rowRange, trackBox,
} from "../../quickcode/frontend/js/trajectory/windowing.js";
import { jsonTokens, prettyJson } from "../../quickcode/frontend/js/json_view.js";

// ---- a tiny log builder --------------------------------------------------

const T0 = Date.parse("2026-09-24T09:00:00");
const iso = (ms) => new Date(ms).toISOString().replace("Z", "");   // what the store writes: no zone

function log() {
  const evs = [];
  const at = (ms, ev) => { evs.push({ seq: evs.length + 1, ts: iso(T0 + ms), turn: 1, ...ev }); return evs; };
  return {
    evs,
    at,
    call: (ms, id, name = "read", extra = {}) => at(ms, { type: "tool_call", id, name, arguments: "{}", ...extra }),
    result: (ms, id, took, extra = {}) => at(ms, { type: "tool_result", id, name: "read", content: "ok", ms: took, ...extra }),
    sub: (ms, agent, ev) => at(ms, { type: "agent_event", agent_id: agent, ev }),
  };
}

// Timestamps are written without a zone and parsed as local time on both
// sides, so tests only ever compare differences.
const rel = (t) => t - T0;

// ---- roles and lanes -----------------------------------------------------

test("every role lands in exactly one lane, and a subagent's tool calls are SUBTOOL beside it", () => {
  const seen = LANES.flatMap((l) => l.roles);
  assert.equal(new Set(seen).size, seen.length);
  assert.equal(roleOf({ type: "agent_event", agent_id: "a", ev: { type: "tool_call" } }), "SUBTOOL");
  assert.equal(roleOf({ type: "agent_event", agent_id: "a", ev: { type: "tool_result" } }), "SUBTOOL");
  assert.equal(roleOf({ type: "agent_event", agent_id: "a", ev: { type: "assistant_message" } }), "AGENT");
  assert.equal(laneOf("SUBTOOL"), LANE_AGENTS);
  assert.equal(laneOf("TOOL"), LANE_TOOLS);
  assert.equal(laneOf("META"), 1);
  assert.equal(roleOf({ type: "usage" }), "META");
});

test("an isolated subagent's worktree record says where its work went", () => {
  const wt = (ev) => ({ type: "agent_event", agent_id: "general-1", ev: { type: "worktree", ...ev } });
  const committed = wt({ action: "committed", branch: "quickcode/general-1-ab12",
    files: 2, insertions: 5, deletions: 1 });
  assert.equal(roleOf(committed), "AGENT");
  assert.equal(previewOf(committed), "worktree committed → quickcode/general-1-ab12 · 2 files +5 −1");
  assert.equal(previewOf(wt({ action: "created", branch: "" })), "worktree created");
  assert.equal(previewOf(wt({ action: "kept", branch: "quickcode/x", detail: "in use" })),
    "worktree kept → quickcode/x · in use");
});

test("a subagent's event links to the definition it was spawned from", () => {
  const defs = new Map([["explore-1", "explore"]]);
  const ev = { type: "agent_event", agent_id: "explore-1", ev: { type: "assistant_message" } };
  assert.deepEqual(configTarget(ev, ev.ev, defs), { href: "#/config/agents/agent.explore", label: "agent.explore" });
  assert.equal(configTarget(ev, ev.ev), null);
  const call = { type: "tool_call", name: "mcp__srv__x" };
  assert.equal(configTarget(call).href, "#/config/parts/tools/tool.mcp__srv__x");
});

// ---- the time axis ---------------------------------------------------------

test("event times fall back sensibly and never run backwards", () => {
  const evs = [{ ts: null }, { ts: iso(T0 + 5000) }, { ts: "not a date" }, { ts: iso(T0 + 4000) }];
  const t = eventTimes(evs).map(rel);
  assert.deepEqual(t, [4999, 5000, 5000, 5000]);
  assert.deepEqual(eventTimes([{}, {}, {}]), [0, 1000, 2000]);
});

test("idle stretches collapse into bands, and the mapping is monotonic across them", () => {
  const spans = [[0, 1000], [2000, 3000], [3000 + 10 * 60000, 3000 + 10 * 60000 + 500]];
  const tMax = spans[2][1];
  const segs = buildSegments(spans, 0, tMax, true);
  assert.equal(segs.filter((s) => s.collapsed).length, 1);
  const band = segs.find((s) => s.collapsed);
  assert.equal(band.r0, 3000);
  assert.equal(band.r1, 3000 + 10 * 60000);
  assert.ok(band.v1 - band.v0 < 1000, "a ten-minute gap takes a narrow band, not ten minutes");
  let prev = -1;
  for (let t = 0; t <= tMax; t += 7919) {
    const v = toVirt(segs, t);
    assert.ok(v >= prev);
    prev = v;
  }
  // Round trip inside the active stretches.
  for (const t of [0, 500, 2500, spans[2][0] + 100]) assert.ok(Math.abs(toReal(segs, toVirt(segs, t)) - t) < 1e-6);
  // True scale keeps every minute.
  const flat = buildSegments(spans, 0, tMax, false);
  assert.equal(flat.length, 1);
  assert.equal(flat[0].v1 - flat[0].v0, tMax);
});

test("only gaps longer than the threshold collapse, and filtered-out work counts as idle", () => {
  const short = buildSegments([[0, 100], [100 + GAP_MIN_MS - 1, 20000]], 0, 20000, true);
  assert.equal(short.filter((s) => s.collapsed).length, 0);
  // The same session, with the middle hidden by a filter: the hole is a gap now.
  const hidden = buildSegments([[0, 100], [59000, 60000]], 0, 60000, true);
  assert.equal(hidden.filter((s) => s.collapsed).length, 1);
  const one = buildSegments([[5, 5]], 5, 5, true);
  assert.deepEqual([one[0].v0, one[0].v1], [0, 1000]);
});

test("a bar that ends where a gap begins stays in the segment it ran in", () => {
  const segs = buildSegments([[0, 1000], [100000, 101000]], 0, 101000, true);
  const home = homeSegment(segs, 1000);
  assert.equal(home.collapsed, false);
  assert.equal(home.r1, 1000);
});

test("zoom keeps the instant under the pointer fixed; fit and reveal stay inside the session", () => {
  const total = 100000;
  const view = { v0: 0, span: total };
  const z = zoomView(view, 300, 1000, 0.5, total);
  const before = view.v0 + 0.3 * view.span;
  const after = z.v0 + 0.3 * z.span;
  assert.ok(Math.abs(before - after) < 1e-6);
  assert.equal(z.span, 50000);
  assert.ok(isFitted(clampView(0, total, total), total));
  assert.ok(!isFitted(z, total));
  // Never zooms out past the whole session, never below the minimum span.
  assert.equal(zoomView(view, 0, 1000, 10, total).span, total);
  assert.equal(zoomView({ v0: 0, span: 5 }, 0, 1000, 0.01, total).span, 4);
  const moved = revealView({ v0: 0, span: 1000 }, 5000, total);
  assert.ok(5000 > moved.v0 && 5000 < moved.v0 + moved.span);
  const centred = revealView({ v0: 0, span: 1000 }, 5000, total, { center: true });
  assert.equal(centred.v0, 4500);
});

test("tick labels never overlap, and the live edge always gets its pill", () => {
  const segs = buildSegments([[T0, T0 + 3600000]], T0, T0 + 3600000, true);
  for (const W of [320, 700, 1500]) {
    const view = { v0: 0, span: 3600000 };
    const { ticks, step } = planTicks({ segs, view, W, live: true, tMin: T0, tMax: T0 + 3600000 });
    assert.ok(step >= pickStep(3600000, W));
    assert.ok(ticks.some((t) => t.kind === "now"), `no now pill at ${W}px`);
    const boxes = ticks.map((t) => {
      const w = t.text.length * 5.9 + 16;
      const lo = t.anchor === "start" ? t.x : t.anchor === "end" ? t.x - w : t.x - w / 2;
      return [lo, lo + w];
    }).sort((a, b) => a[0] - b[0]);
    for (let i = 1; i < boxes.length; i++) assert.ok(boxes[i][0] >= boxes[i - 1][1], `overlap at ${W}px`);
  }
});

test("the clock after a collapsed gap is labelled, so the axis cannot hide the gap", () => {
  const segs = buildSegments([[T0, T0 + 5000], [T0 + 3600000, T0 + 3605000]], T0, T0 + 3605000, true);
  const total = segs[segs.length - 1].v1;
  const { ticks } = planTicks({ segs, view: { v0: 0, span: total }, W: 900, live: false,
    tMin: T0, tMax: T0 + 3605000 });
  assert.ok(ticks.some((t) => t.kind === "resume" && t.t === T0 + 3600000));
});

// ---- the model ------------------------------------------------------------

test("a tool call spans to its own agent's result, and a subagent's id never pairs with the main agent's", () => {
  const l = log();
  l.call(0, "c1");
  l.sub(100, "explore-1", { type: "tool_call", id: "c1", name: "read", arguments: "{}" });
  l.sub(400, "explore-1", { type: "tool_result", id: "c1", name: "read", content: "sub", ms: 250 });
  l.result(2000, "c1", 1900);
  const m = buildModel(l.evs, { now: T0 + 5000 });
  const [main, sub] = [m.bySeq.get(1), m.bySeq.get(2)];
  assert.equal(rel(main.t1), 2000);
  assert.equal(main.result.content, "ok");
  assert.equal(rel(sub.t1), 400);
  assert.equal(sub.result.content, "sub");
  assert.equal(m.bySeq.get(3).call, sub, "a result knows its call");
  assert.equal(sub.role, "SUBTOOL");
});

test("an unanswered call runs to now while live and stays a point once settled", () => {
  const l = log();
  l.call(0, "c1");
  const live = buildModel(l.evs, { now: T0 + 8000, live: true });
  assert.ok(live.items[0].running);
  assert.equal(rel(live.items[0].t1), 8000);
  assert.equal(live.open.length, 1);
  const settled = buildModel(l.evs, { now: T0 + 8000, live: false });
  assert.ok(!settled.items[0].running);
  assert.equal(settled.items[0].t1, settled.items[0].t0);
  // The ticker moves the live edge without a rebuild.
  assert.ok(advanceLive(live, T0 + 9000));
  assert.equal(rel(live.items[0].t1), 9000);
  assert.equal(rel(live.tMax), 9000);
  assert.ok(!advanceLive(settled, T0 + 9000));
});

test("a model turn is inferred to start at the previous event, capped", () => {
  const l = log();
  l.at(0, { type: "user_message", text: "hi" });
  l.at(4000, { type: "assistant_message", text: "yo", finish_reason: "stop" });
  l.at(4000 + INFERRED_CAP_MS + 60000, { type: "assistant_message", text: "late", finish_reason: "stop" });
  const m = buildModel(l.evs, { collapseGaps: false });
  const a = m.items[1];
  assert.equal(rel(a.t0), 0);
  assert.ok(a.inferred);
  const late = m.items[2];
  assert.equal(late.t1 - late.t0, INFERRED_CAP_MS);
});

test("a spawned agent's bar lasts until the last thing it did", () => {
  const l = log();
  l.at(0, { type: "agent_spawned", agent_id: "a", definition: "explore" });
  l.sub(3000, "a", { type: "assistant_message", text: "done", finish_reason: "stop" });
  const m = buildModel(l.evs, {});
  assert.equal(rel(m.items[0].t1), 3000);
  assert.equal(m.agentDefs.get("a"), "explore");
});

test("rebuilding after an append reuses the earlier items and their index", () => {
  const l = log();
  l.call(0, "c1");
  const first = buildModel(l.evs, { live: true, now: T0 + 50 });
  l.result(300, "c1", 290);
  const second = buildModel(l.evs, { live: false, prev: first });
  assert.equal(second.items[0], first.items[0], "same object for the same event");
  assert.equal(second.bySeq.get(2), second.items[1]);
  assert.equal(second.items[0].result, l.evs[1]);
  assert.ok(!second.items[0].running);
  // A different log with the same length is not an append.
  const other = log();
  other.call(0, "x");
  other.result(10, "x", 5);
  const third = buildModel(other.evs, { prev: second });
  assert.notEqual(third.items[0], second.items[0]);
  assert.equal(third.bySeq.get(1).ev, other.evs[0]);
});

// ---- tracks ------------------------------------------------------------------

test("parallel tool calls stack into tracks, results sit on their call's, sequential ones share", () => {
  const l = log();
  l.call(0, "a"); l.call(0, "b"); l.call(0, "c");
  l.result(100, "a", 100); l.result(900, "b", 900); l.result(400, "c", 400);
  l.call(1000, "d"); l.result(1100, "d", 100);
  const m = buildModel(l.evs, {});
  const track = (seq) => m.bySeq.get(seq).track;
  assert.deepEqual([track(1), track(2), track(3)], [0, 1, 2]);
  assert.deepEqual([track(4), track(5), track(6)], [0, 1, 2]);
  assert.equal(track(7), 0, "a later call reuses the first free track");
  assert.equal(m.tracks[LANE_TOOLS], 3);
  assert.equal(m.tracks[0], 1);
});

test("parallel subagents each get a track, and their own tool calls land on it", () => {
  const l = log();
  l.at(0, { type: "agent_spawned", agent_id: "x", definition: "explore" });
  l.at(10, { type: "agent_spawned", agent_id: "y", definition: "explore" });
  l.sub(100, "x", { type: "tool_call", id: "1", name: "read", arguments: "{}" });
  l.sub(150, "y", { type: "tool_call", id: "1", name: "grep", arguments: "{}" });
  l.sub(300, "x", { type: "tool_result", id: "1", name: "read", content: "", ms: 200 });
  l.sub(350, "y", { type: "tool_result", id: "1", name: "grep", content: "", ms: 200 });
  l.at(400, { type: "agent_done", agent_id: "x", status: "done" });
  l.at(450, { type: "agent_done", agent_id: "y", status: "done" });
  l.at(1000, { type: "agent_spawned", agent_id: "z", definition: "general" });
  const m = buildModel(l.evs, {});
  const byAgent = {};
  for (const it of m.items) (byAgent[it.ev.agent_id] ||= new Set()).add(it.track);
  assert.deepEqual([...byAgent.x], [0]);
  assert.deepEqual([...byAgent.y], [1]);
  assert.deepEqual([...byAgent.z], [0], "an agent that starts after both ended reuses track 0");
  assert.equal(m.tracks[LANE_AGENTS], 2);
});

test("more parallel work than the lane can split shares the last track", () => {
  const l = log();
  for (let i = 0; i < 6; i++) l.call(0, "c" + i);
  for (let i = 0; i < 6; i++) l.result(1000, "c" + i, 1000);
  const m = buildModel(l.evs, {});
  assert.equal(m.tracks[LANE_TOOLS], MAX_TRACKS);
  assert.ok(m.items.every((it) => it.track < MAX_TRACKS));
});

// ---- windowing ----------------------------------------------------------------

test("the row window covers the viewport plus overscan and never leaves the list", () => {
  assert.deepEqual(rowRange(0, 240, 24, 10000, 8), { first: 0, last: 18 });
  assert.deepEqual(rowRange(24000, 240, 24, 10000, 8), { first: 992, last: 1018 });
  assert.deepEqual(rowRange(1e9, 240, 24, 50, 8), { first: 50, last: 50 });
  assert.deepEqual(rowRange(0, 240, 24, 0, 8), { first: 0, last: 0 });
  assert.equal(bottomTop(100, 24, 240), 2160);
  assert.equal(bottomTop(3, 24, 240), 0);
});

test("tracks split the lane and the pointer maps back onto the same track", () => {
  for (const n of [1, 2, 3]) {
    for (let k = 0; k < n; k++) {
      const box = trackBox(k, n, 20);
      assert.ok(box.top >= 0 && box.top + box.height <= 20);
      const hit = laneTrackAt(2 * 20 + box.top + box.height / 2, 20, [1, 1, n, 1]);
      assert.deepEqual(hit, { lane: 2, track: k });
    }
  }
  assert.equal(laneTrackAt(-5, 20, [1, 1, 1, 1]).lane, 0);
  assert.equal(laneTrackAt(500, 20, [1, 1, 1, 1]).lane, 3);
});

function synthetic(n) {
  const l = log();
  let t = 0;
  for (let i = 0; l.evs.length < n; i++) {
    t += 700;
    l.at(t, { type: "assistant_message", text: "x", finish_reason: "tool_calls" });
    l.call(t, "c" + i);
    t += 300;
    l.result(t, "c" + i, 300);
  }
  return l.evs.slice(0, n);
}

test("a 10k-event session paints a bounded number of bars and rows", () => {
  const m = buildModel(synthetic(10000), {});
  assert.equal(m.items.length, 10000);
  const W = 1200;
  const fitted = cullBars(m.items, { view: { v0: 0, span: m.totalV }, W, laneCount: LANES.length });
  const nodes = fitted.bars.reduce((s, b) => s + b.length, 0);
  assert.ok(nodes <= LANES.length * W / MIN_BAR_PX, `too many bar nodes: ${nodes}`);
  assert.ok(nodes < 1000, `sub-pixel bars should merge into runs, got ${nodes}`);
  assert.ok(fitted.hits.length <= 2400);
  const zoomed = cullBars(m.items, { view: { v0: m.totalV / 2, span: 20000 }, W, laneCount: LANES.length });
  assert.ok(zoomed.hits.length < 100, "a zoomed view culls to what it shows");
  for (const lane of zoomed.bars) {
    for (const b of lane) assert.ok(b.x0 >= -16 && b.x1 <= W + 16, "bars are clipped to the viewport");
  }
  const { first, last } = rowRange(123456, 600, 24, m.items.length);
  assert.ok(last - first <= Math.ceil(600 / 24) + 17);
});

test("a selected, running or failed bar is drawn on its own and on top", () => {
  const l = log();
  for (let i = 0; i < 20; i++) { l.call(i, "c" + i); l.result(i + 1, "c" + i, 1, { is_error: i === 7 }); }
  const m = buildModel(l.evs, {});
  const { bars } = cullBars(m.items, { view: { v0: 0, span: m.totalV * 100 }, W: 800,
    laneCount: LANES.length, selectedSeq: 3 });
  const tools = bars[LANE_TOOLS];
  assert.ok(tools.length < 10, "the plain bars merged");
  assert.equal(tools[tools.length - 1].sel, true, "selection is painted last");
  assert.ok(tools.some((b) => b.err), "the failure is not merged away");
});

test("the pointer picks the narrowest bar under it, on its own track first", () => {
  const it = (seq, lane, track) => ({ seq, lane, track });
  const hits = [
    { it: it(1, 3, 0), x0: 0, x1: 500, lane: 3, track: 0 },     // an agent's lifetime
    { it: it(2, 3, 0), x0: 100, x1: 110, lane: 3, track: 0 },   // its tool call
    { it: it(3, 3, 1), x0: 104, x1: 106, lane: 3, track: 1 },
  ];
  assert.equal(hitTest(hits, 105, { lane: 3, track: 0 }).it.seq, 2);
  assert.equal(hitTest(hits, 105, { lane: 3, track: 1 }).it.seq, 3);
  assert.equal(hitTest(hits, 300, { lane: 3, track: 1 }).it.seq, 1);
  assert.equal(hitTest(hits, 300, { lane: 0, track: 0 }).it.seq, 1, "an empty lane falls back to what is near");
  assert.equal(hitTest([], 10, { lane: 0, track: 0 }), null);
});

// ---- JSON for the inspector -------------------------------------------------------

test("JSON tokens cover the text exactly and classify keys, strings, numbers and literals", () => {
  const src = JSON.stringify({ a: "<img src=x onerror=alert(1)>", n: -1.5e3, t: true, z: null, q: "say \"hi\"" }, null, 2);
  const toks = jsonTokens(src);
  assert.equal(toks.map((t) => t.text).join(""), src);
  const cls = (text) => toks.find((t) => t.text === text)?.cls;
  assert.equal(cls('"a"'), "j-key");
  assert.equal(cls('"<img src=x onerror=alert(1)>"'), "j-str");
  assert.equal(cls("-1.5e3") ?? cls("-1500"), "j-num");
  assert.equal(cls("true"), "j-lit");
  assert.equal(cls('"say \\"hi\\""'), "j-str");
  assert.equal(prettyJson('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(prettyJson("12345"), null);
  assert.equal(prettyJson("{broken"), null);
});
