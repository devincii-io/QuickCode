// The trajectory's derived model: one item per logged event with a real
// [t0, t1] span, its lane and track, and its position on the virtual axis.
// Pure — the caller hands in the events, the clock and the filter.

import { LANE_AGENTS, LANE_TOOLS, LANES, innerOf, laneOf, roleOf } from "./roles.js";
import { buildSegments, eventTimes, homeSegment, toVirt } from "./timeaxis.js";

// A span inferred from "the previous event happened at T" is capped, so a pause
// in the log never inflates into a three-minute model bar.
export const INFERRED_CAP_MS = 180000;
// Parallel work stacks into tracks inside its lane; past this many the lane
// is too thin to split further and the rest share the last track.
export const MAX_TRACKS = 3;

// An empty log still needs a coordinate system, or every mapping would have to
// special-case "no events yet".
export function emptyModel(now = Date.now()) {
  return {
    items: [], bySeq: new Map(), agentDefs: new Map(), open: [], live: false,
    segs: [{ i: 0, r0: now, r1: now + 1000, v0: 0, v1: 1000, collapsed: false }],
    tMin: now, tMax: now + 1000, totalV: 1000, tracks: LANES.map(() => 1),
  };
}

// Call ids are only unique within one agent's stream: matching on the bare id
// paired delegated calls with the wrong result. So results are indexed per
// agent ("" is the main agent), then by id.
function resultIndex(evs) {
  const byAgent = new Map();
  for (let i = 0; i < evs.length; i++) {
    const ev = evs[i];
    const inner = innerOf(ev);
    if (inner.type !== "tool_result" || inner.id == null) continue;
    const agent = ev.agent_id ?? "";
    let ids = byAgent.get(agent);
    if (!ids) { ids = new Map(); byAgent.set(agent, ids); }
    ids.set(inner.id, i);
  }
  return (ev, id) => byAgent.get(ev.agent_id ?? "")?.get(id);
}

/**
 * @param {object[]} evs      the logged events, in log order
 * @param {object}   opts
 * @param {number}   opts.now           wall clock, for bars still running
 * @param {boolean}  opts.live          whether the right edge is still moving
 * @param {(ev)=>boolean} opts.isShown  the filters and the search box
 * @param {boolean}  opts.collapseGaps  collapse idle stretches on the axis
 * @param {object}   [opts.prev]        the previous model: its items are
 *   reused for the same events, since a live session rebuilds on every append
 *   and ten thousand fresh objects a time was mostly garbage collection
 */
export function buildModel(evs, { now = Date.now(), live = false, isShown = () => true,
  collapseGaps = true, prev = null } = {}) {
  const n = evs.length;
  if (!n) return emptyModel(now);

  const raw = eventTimes(evs);
  const resultAt = resultIndex(evs);
  const agentEnd = new Map();
  const agentDefs = new Map();
  for (let i = 0; i < n; i++) {
    const ev = evs[i];
    if (ev.agent_id) agentEnd.set(ev.agent_id, raw[i]);
    if (ev.type === "agent_spawned" && ev.agent_id && ev.definition) {
      agentDefs.set(ev.agent_id, ev.definition);
    }
  }

  const old = prev?.items;
  const items = new Array(n);
  // An append leaves every earlier item in place, and then the previous index
  // is still right for all of them; anything else rebuilds it.
  let reused = !!old && old.length <= n;
  const bySeq = reused ? prev.bySeq : new Map();
  const open = [];
  const agentPrev = new Map();
  let tMax = -Infinity;
  for (let i = 0; i < n; i++) {
    const ev = evs[i];
    const inner = innerOf(ev);
    let t0 = raw[i];
    let t1 = raw[i];
    let inferred = false;
    let running = false;
    let result = null;
    let resIdx = -1;

    if (inner.type === "tool_call") {
      const j = inner.id != null ? resultAt(ev, inner.id) : undefined;
      if (j != null) {
        resIdx = j;
        result = innerOf(evs[j]);
        t1 = Math.max(raw[j], t0 + (result.ms || 0));
      } else if (live) {
        t1 = now; running = true;
      }
      // A call with no result in a settled session never finished. Drawing it
      // out to "now" would invent a duration nobody measured.
    } else if (inner.type === "assistant_message") {
      // The record lands when the round completes, so the model was busy from
      // whatever happened last (in its own stream) until now.
      const prevI = ev.type === "agent_event" && ev.agent_id
        ? (agentPrev.get(ev.agent_id) ?? i - 1)
        : i - 1;
      if (prevI >= 0) { t0 = raw[prevI]; inferred = true; }
    } else if (ev.type === "agent_spawned") {
      t1 = Math.max(t1, agentEnd.get(ev.agent_id) ?? t1);
    }
    if (inferred && t1 - t0 > INFERRED_CAP_MS) t0 = t1 - INFERRED_CAP_MS;
    if (t1 < t0) t1 = t0;
    if (ev.agent_id) agentPrev.set(ev.agent_id, i);
    if (t1 > tMax) tMax = t1;

    let it = old?.[i];
    if (!it || it.ev !== ev) {
      if (it) reused = false;
      const role = roleOf(ev);
      it = { i, seq: ev.seq, ev, inner, role, lane: laneOf(role), track: 0,
        t0: 0, t1: 0, running: false, result: null, call: null, inferred: false,
        err: !!(inner.is_error || inner.type === "error"),
        shown: true, v0: 0, v1: 0, row: -1 };
    }
    it.t0 = t0; it.t1 = t1; it.running = running; it.inferred = inferred;
    it.result = result; it.call = null; it.track = 0; it.row = -1;
    // Decided here because the segment layout depends on it: the gaps worth
    // collapsing are the gaps in what the filters actually left on screen.
    it.shown = isShown(ev);
    it._res = resIdx;
    items[i] = it;
    if (running) open.push(it);
  }
  for (const it of items) if (it._res >= 0) items[it._res].call = it;
  if (!reused) bySeq.clear();
  for (let i = reused ? old.length : 0; i < n; i++) bySeq.set(items[i].seq, items[i]);

  const tMin = Math.min(raw[0], items[0].t0);
  if (tMax < tMin) tMax = tMin;
  if (live && now > tMax) tMax = now;

  const spans = [];
  for (const it of items) if (it.shown) spans.push([it.t0, it.t1]);
  const segs = buildSegments(spans, tMin, tMax, collapseGaps);
  for (const it of items) {
    // Pinned into the segment its end lives in, so nothing is drawn straddling
    // a collapsed band.
    const s = homeSegment(segs, it.t1);
    if (it.t0 < s.r0) it.t0 = s.r0;
    it.v0 = toVirt(segs, it.t0);
    it.v1 = Math.max(it.v0, toVirt(segs, it.t1));
  }

  const tracks = assignTracks(items);
  const lastSeg = segs[segs.length - 1];
  return {
    items, bySeq, segs, agentDefs, open, live, tracks,
    tMin, tMax, totalV: Math.max(1, lastSeg.v1),
  };
}

// First-fit interval partitioning: each span takes the lowest track whose last
// occupant ended before it starts. Visit spans in order of start.
function packer() {
  const ends = [];
  return {
    place(a, b) {
      let t = 0;
      while (t < ends.length && ends[t] >= a) t++;
      ends[t] = b;
      return t;
    },
    get count() { return Math.max(1, ends.length); },
  };
}

/**
 * Stack parallel work into tracks so nothing hides behind a longer bar.
 *
 * Tools: the main agent's concurrent calls each get a track, and a result sits
 * on its call's. Agents: one track per concurrently running subagent, and
 * every event it emits — its tool calls included — lands on that track, so two
 * parallel explorers read as two rows instead of one smear. Input and Model
 * are sequential by nature and keep a single track. Mutates `it.track`;
 * returns the track count per lane, capped at MAX_TRACKS.
 */
export function assignTracks(items) {
  const counts = LANES.map(() => 1);
  const cap = (t) => (t < MAX_TRACKS ? t : MAX_TRACKS - 1);

  // The main agent's calls start in log order, which is clock order: no sort.
  const tools = packer();
  const agentLife = new Map();   // agent_id -> [first t0, last t1]
  for (const it of items) {
    if (!it.shown) continue;
    if (it.lane === LANE_TOOLS && it.inner.type === "tool_call") {
      it.track = cap(tools.place(it.t0, it.t1));
    } else if (it.lane === LANE_AGENTS && it.ev.agent_id) {
      const span = agentLife.get(it.ev.agent_id);
      if (!span) agentLife.set(it.ev.agent_id, [it.t0, it.t1]);
      else {
        if (it.t0 < span[0]) span[0] = it.t0;
        if (it.t1 > span[1]) span[1] = it.t1;
      }
    }
  }
  counts[LANE_TOOLS] = cap(tools.count - 1) + 1;

  const agents = [...agentLife].sort((a, b) => a[1][0] - b[1][0]);
  const pack = packer();
  const agentTrack = new Map();
  for (const [id, [a, b]] of agents) agentTrack.set(id, cap(pack.place(a, b)));
  counts[LANE_AGENTS] = cap(pack.count - 1) + 1;

  for (const it of items) {
    if (it.lane === LANE_TOOLS && it.inner.type === "tool_result") {
      it.track = it.call ? it.call.track : 0;
    } else if (it.lane === LANE_AGENTS) {
      it.track = agentTrack.get(it.ev.agent_id) ?? 0;
    }
  }
  return counts;
}

/**
 * Move the live edge to `now` without rebuilding. A passing second changes
 * only the last segment and the bars still open; rebuilding four times a
 * second would be O(n log n) in a log that can hold thousands of events.
 * Returns false when nothing moved.
 */
export function advanceLive(model, now) {
  if (!model.live) return false;
  const last = model.segs[model.segs.length - 1];
  // A collapsed tail only happens when a filter hid everything recent; the next
  // rebuild sorts it out, and stretching a hatched band would misreport it.
  if (last.collapsed || now <= last.r1) return false;
  last.r1 = now;
  last.v1 = last.v0 + (last.r1 - last.r0);
  model.tMax = now;
  model.totalV = Math.max(1, last.v1);
  for (const it of model.open) {
    it.t1 = now;
    it.v1 = Math.max(it.v0, toVirt(model.segs, now));
  }
  return true;
}
