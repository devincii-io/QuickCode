// Trajectory view: a real time-axis timeline over the append-only event log.
//
// Four lanes (Input / Model / Tools / Agents) of duration bars sit on a
// wall-clock x axis with collapsible idle gaps, a hover crosshair with a rich
// card, and a persistent playhead. Below them the dense event table — windowed,
// selection-synced with the lanes both ways — and the right-hand inspector
// (Summary / Payload / Result / Timing), which is unchanged.
//
// Nothing here keeps a DOM node per event: both the lanes and the table paint
// only what the current viewport shows, so a 10k-event session stays cheap.

import { store, subscribe, toolResultFor } from "./store.js";
import { debounce, esc, fmtMs, fmtTime, fmtTokens, oneLine } from "./util.js";

const ROLES = ["SYSTEM", "USER", "CONTEXT", "ASSISTANT", "TOOL", "REVIEW", "AGENT", "ERROR"];

// The four lanes the redesign asks for. Every role lands in exactly one of
// them; META (usage, mode/model changes, compaction) is model bookkeeping.
const LANES = [
  { key: "input", label: "Input", roles: ["USER", "SYSTEM", "CONTEXT", "REVIEW"] },
  { key: "model", label: "Model", roles: ["ASSISTANT", "META", "ERROR"] },
  { key: "tools", label: "Tools", roles: ["TOOL"] },
  { key: "agents", label: "Agents", roles: ["AGENT"] },
];
const LANE_OF = {};
LANES.forEach((l, i) => l.roles.forEach((r) => { LANE_OF[r] = i; }));

// An idle stretch longer than this (and than 2% of the session) is collapsed.
const GAP_MIN_MS = 15000;
// A span inferred from "the previous event happened at T" is capped here, so a
// pause in the log never inflates into a three-minute model bar.
const INFERRED_CAP_MS = 180000;
const MIN_BAR_PX = 3;       // a zero-duration event still needs to be clickable
const HIT_SLOP_PX = 5;
const LANE_OVERSCAN_PX = 240;
const MAX_BARS = 2400;      // backstop; pixel dedup normally keeps us far below
const ROW_OVERSCAN = 8;
const TICK_TARGET_PX = 96;

let table, detail, detailBody, detailTitle, searchBox, followBtn;
let timelineEl, axisEl, plotEl, gutterEl, bandsEl, cursorEl, playEl, hoverEl;
let spacerEl, rowsEl;
let laneEls = [];

let activeFilters = new Set(ROLES);
let query = "";
let selectedSeq = null;
let detailTab = "summary";
let collapseGaps = true;

// Live-follow: on by default, paused by any manual scroll-up, pan or row click,
// and only ever resumed by the toolbar button (which jumps to the newest event).
let following = true;
let autoScrollUntil = 0;     // our own scrolls must not read as "user scrolled"

// ---- derived state -------------------------------------------------------

let model = emptyModel();
let rows = [];               // the filtered events the table paints from
let hits = [];               // last painted bars, for geometry hit-testing
let view = { v0: 0, span: 1000 };
let playV = null;            // playhead position, in virtual ms
let rowH = 24;
let dirty = 0;               // bit 1 = model, bit 2 = lanes, bit 4 = rows
let frame = 0;

// An empty log still needs a coordinate system, or every mapping below would
// have to special-case "no events yet".
function emptyModel() {
  const t = Date.now();
  return {
    items: [], bySeq: new Map(), agentDefs: new Map(),
    segs: [{ r0: t, r1: t + 1000, v0: 0, v1: 1000, collapsed: false }],
    tMin: t, tMax: t + 1000, totalV: 1000,
  };
}

// ---- classification (unchanged semantics) --------------------------------

export function roleOf(ev) {
  switch (ev.type === "agent_event" ? "agent_event" : ev.type) {
    case "system_prompt": return "SYSTEM";
    case "user_message": return "USER";
    case "context_injection": return "CONTEXT";
    case "assistant_message": return "ASSISTANT";
    case "tool_call": case "tool_result": return "TOOL";
    case "permission_request": case "permission_resolved":
    case "plan_request": case "plan_resolved": return "REVIEW";
    case "agent_event": case "agent_spawned": case "agent_done": return "AGENT";
    case "error": return "ERROR";
    default: return "META";
  }
}

function previewOf(ev) {
  const inner = ev.type === "agent_event" ? (ev.ev || {}) : ev;
  switch (inner.type) {
    case "system_prompt": return "System prompt · " + oneLine(inner.text, 160);
    case "user_message": return oneLine(inner.text, 200);
    case "context_injection": return oneLine(inner.text, 200);
    case "assistant_message":
      return oneLine(inner.text || (inner.reasoning ? "(reasoning only)" : ""), 200);
    case "tool_call": return `${inner.name}(${oneLine(inner.arguments, 160)})`;
    case "tool_result":
      return `${inner.name} → ${oneLine(inner.content, 160)}`;
    case "permission_request": return `ask: ${inner.tool}(${oneLine(inner.arg, 120)})`;
    case "permission_resolved":
      return `${inner.allow ? "allowed" : "denied"}: ${inner.tool}(${oneLine(inner.arg, 110)})`;
    case "plan_request": return "plan submitted for review";
    case "plan_resolved": return inner.approved ? `plan approved → ${inner.mode_after}` : "plan revision requested";
    case "mode_changed": return `mode → ${inner.mode}`;
    case "model_changed": return `model → ${inner.model}`;
    case "compacted": return `compacted (${inner.summary_chars} char summary)`;
    case "composition_changed": return compositionPreview(inner);
    case "profile_changed": return profilePreview(inner);
    case "agent_spawned": return `spawned ${ev.agent_id} (${ev.definition})`;
    case "system_note": return inner.text;
    case "usage": return `tokens in ${fmtTokens(inner.input_tokens)} / out ${fmtTokens(inner.output_tokens)}`;
    case "error": return inner.message;
    default: return oneLine(JSON.stringify(inner), 160);
  }
}

// A composition switch is the single most consequential row in the log — the
// agent's tools, ceiling and delegation all changed under a live conversation —
// and it used to render as raw JSON because it had no case above. The event
// already carries the answer; this is only the sentence.
function compositionPreview(inner) {
  const n = (inner.tools || []).length;
  const moved = [];
  if (inner.gained?.length) moved.push("+" + inner.gained.join(", "));
  if (inner.lost?.length) moved.push("−" + inner.lost.join(", "));
  const from = inner.from_preset && inner.from_preset !== inner.preset
    ? ` (was ${inner.from_preset})` : "";
  return `composition → ${inner.title || inner.preset}${from} · ${n} tool${
    n === 1 ? "" : "s"} · ceiling ${inner.ceiling}` +
    (moved.length ? " · " + moved.join(" · ") : " · same tools") +
    (inner.spawns?.length ? ` · spawns ${inner.spawns.join(", ")}` : " · no delegation");
}

// A posture switch changes what the next tool call will be allowed to do
// without asking, which makes it the row that explains every permission event
// after it. The counts are the whole engine state, so they are what it says.
function profilePreview(inner) {
  if (!inner.profile) return "permission profile cleared";
  return `permission profile → ${inner.title || inner.profile} · mode ${
    inner.mode} · ${inner.allow} allow · ${inner.ask} ask · ${inner.deny} deny`;
}

// ---- cross-links into #/config/… ------------------------------------------
//
// A tool call in the trajectory should be one click from the card that governs
// it. The configuration view is a real view addressed by `#/config/…`, and
// main.js's hashchange listener shows it from anywhere — so this is a plain
// href, not a callback, and it survives being copied out of the log.

const enc = encodeURIComponent;

function toolTarget(name) {
  return name ? { href: `#/config/parts/tools/${enc("tool." + name)}`,
                  label: `tool.${name}` } : null;
}

function agentTarget(definition) {
  return definition ? { href: `#/config/agents/${enc("agent." + definition)}`,
                        label: `agent.${definition}` } : null;
}

/** The configuration page that governs one event, or null when nothing does. */
export function configTarget(ev, inner = ev) {
  switch (inner.type || ev.type) {
    case "tool_call": case "tool_result":
      return toolTarget(inner.name);
    case "permission_request": case "permission_resolved":
      return toolTarget(inner.tool);
    case "agent_spawned":
      return agentTarget(ev.definition || inner.definition);
    case "composition_changed":
      return inner.preset
        ? { href: `#/config/compositions/${enc(inner.preset)}`, label: inner.preset }
        : null;
    case "profile_changed":
      return inner.profile
        ? { href: `#/config/profiles/${enc(inner.profile)}`, label: inner.profile }
        : { href: "#/config/profiles", label: "profiles" };
    case "system_prompt": case "context_injection":
      return { href: "#/config/parts/prompt", label: "prompt" };
    case "model_changed":
      return { href: "#/config/parts/models", label: "models" };
    case "mode_changed": case "permission_denied":
      return { href: "#/config/parts/policies/runtime.permissions",
               label: "runtime.permissions" };
    case "compacted":
      return { href: "#/config/parts/policies/runtime.compaction",
               label: "runtime.compaction" };
    default:
      // Anything a subagent emitted points at the definition it was spawned
      // from, which is the page that decided what it was allowed to do.
      return ev.agent_id ? agentTarget(model.agentDefs?.get(ev.agent_id)) : null;
  }
}

function configLinkHtml(ev, inner) {
  const target = configTarget(ev, inner);
  if (!target) return "";
  return `<a class="t-ms k-link tj-cfg" href="${esc(target.href)}"
    title="Open ${esc(target.label)} in configuration — this is what governs it"
    >${esc(target.label)} ↗</a>`;
}

function visible(ev) {
  const role = roleOf(ev);
  if (!activeFilters.has(role) && role !== "META") return false;
  if (role === "META" && activeFilters.size !== ROLES.length) return false;
  if (query && !JSON.stringify(ev).toLowerCase().includes(query)) return false;
  return true;
}

// ---- boot ----------------------------------------------------------------

export function initTrajectory() {
  table = document.getElementById("traj-table");
  spacerEl = document.getElementById("tj-spacer");
  rowsEl = document.getElementById("tj-rows");
  timelineEl = document.getElementById("traj-timeline");
  axisEl = document.getElementById("tj-axis");
  plotEl = document.getElementById("tj-plot");
  gutterEl = document.getElementById("tj-gutter");
  bandsEl = document.getElementById("tj-bands");
  cursorEl = document.getElementById("tj-cursor");
  playEl = document.getElementById("tj-playhead");
  hoverEl = document.getElementById("tj-hover");
  detail = document.getElementById("traj-detail");
  detailBody = document.getElementById("detail-body");
  detailTitle = document.getElementById("traj-detail-title");
  searchBox = document.getElementById("traj-search");
  followBtn = document.getElementById("traj-follow");

  buildLaneScaffold();
  wireToolbar();
  wireTable();
  wirePlot();

  subscribe((kind, ev) => {
    // A new conversation starts at the live edge again.
    if (kind === "reset") {
      selectedSeq = null; playV = null; detail.classList.add("hidden");
      setFollowing(true); renderAll(); return;
    }
    if (kind === "replay_done") { renderAll(); fit(); return; }
    if (kind === "event" && !store.replaying) appendEvent(ev);
  });

  setFollowing(true);
  renderAll();
  fit();
}

function buildLaneScaffold() {
  gutterEl.innerHTML = LANES.map((l) =>
    `<div class="tj-glabel" data-lane="${l.key}"><span>${l.label}</span></div>`).join("");
  plotEl.querySelectorAll(".tj-lane").forEach((n) => n.remove());
  laneEls = LANES.map((l) => {
    const n = document.createElement("div");
    n.className = "tj-lane";
    n.dataset.lane = l.key;
    plotEl.insertBefore(n, bandsEl);
    return n;
  });
}

function wireToolbar() {
  const filters = document.getElementById("traj-filters");
  for (const role of ROLES) {
    const b = document.createElement("button");
    b.className = "tfilter active";
    b.style.color = `var(--chip-${role.toLowerCase()})`;
    b.textContent = role;
    b.addEventListener("click", () => {
      if (activeFilters.has(role) && activeFilters.size === ROLES.length) {
        activeFilters = new Set([role]); // first click isolates
      } else if (activeFilters.has(role)) {
        activeFilters.delete(role);
        if (!activeFilters.size) activeFilters = new Set(ROLES);
      } else {
        activeFilters.add(role);
      }
      filters.querySelectorAll(".tfilter").forEach((btn, i) =>
        btn.classList.toggle("active", activeFilters.has(ROLES[i])));
      renderAll();
    });
    filters.appendChild(b);
  }

  searchBox.addEventListener("input", debounce(() => {
    query = searchBox.value.toLowerCase();
    renderAll();
  }, 120));

  followBtn.addEventListener("click", () => setFollowing(!following, { jump: true }));
  document.getElementById("traj-fit").addEventListener("click", () => fit());
  const gapBtn = document.getElementById("traj-gaps");
  gapBtn.addEventListener("click", () => {
    collapseGaps = !collapseGaps;
    gapBtn.classList.toggle("is-on", collapseGaps);
    gapBtn.setAttribute("aria-pressed", collapseGaps ? "true" : "false");
    renderAll();
    fit();
  });
  document.getElementById("traj-export").addEventListener("click", exportLog);

  document.getElementById("traj-detail-close").addEventListener("click", () => {
    selectedSeq = null;
    detail.classList.add("hidden");
    invalidate(6);
  });
  document.getElementById("detail-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    detailTab = b.dataset.tab;
    document.querySelectorAll("#detail-tabs button").forEach((x) =>
      x.classList.toggle("active", x === b));
    renderDetail();
  });
}

function wireTable() {
  table.addEventListener("scroll", () => {
    invalidate(4);
    // Scrolling away from the newest row is the gesture that means "let me read".
    if (!following || Date.now() < autoScrollUntil || !table.clientHeight) return;
    if (!atBottom()) setFollowing(false);
  });
  const activate = (e) => {
    // The row's config link is a real navigation, not a selection: letting it
    // do both would open configuration *and* rearrange the timeline behind it.
    if (e.target.closest("a")) return;
    const row = e.target.closest(".tj-row");
    if (!row) return;
    selectSeq(Number(row.dataset.seq), { center: true });
  };
  rowsEl.addEventListener("click", activate);
  rowsEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(e); }
  });
  // A hidden pane has no scroll height, so rows that arrived while the panel
  // was closed leave it parked at the top: re-anchor once it gets a size.
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      measure();
      invalidate(6);
      if (following) scrollToNewest();
    }).observe(table);
    new ResizeObserver(() => invalidate(2)).observe(plotEl);
  }
}

function wirePlot() {
  plotEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = plotEl.getBoundingClientRect();
    const horizontal = e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY);
    if (horizontal) {
      // A trackpad's horizontal flick is a pan, and panning is "let me read".
      panBy(((e.deltaX || e.deltaY) / Math.max(1, rect.width)) * view.span);
      setFollowing(false);
    } else {
      // Zoom keeps follow alive: you are changing the lens, not looking away.
      const k = Math.exp((e.deltaY * (e.ctrlKey ? 2.2 : 1)) * 0.0022);
      zoomAt(e.clientX - rect.left, k);
    }
  }, { passive: false });

  let drag = null;
  plotEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const rect = plotEl.getBoundingClientRect();
    drag = { x: e.clientX, y: e.clientY, moved: 0, v0: view.v0, w: rect.width };
    plotEl.setPointerCapture(e.pointerId);
    plotEl.classList.add("is-dragging");
  });
  plotEl.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x;
      drag.moved = Math.max(drag.moved, Math.abs(dx), Math.abs(e.clientY - drag.y));
      if (drag.moved > 3) {
        setView(drag.v0 - (dx / Math.max(1, drag.w)) * view.span, view.span);
        setFollowing(false);
      }
      return;
    }
    onHover(e);
  });
  const endDrag = (e) => {
    if (!drag) return;
    const moved = drag.moved;
    drag = null;
    plotEl.classList.remove("is-dragging");
    try { plotEl.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
    if (moved <= 3) onPlotClick(e);
  };
  plotEl.addEventListener("pointerup", endDrag);
  plotEl.addEventListener("pointercancel", endDrag);
  plotEl.addEventListener("pointerleave", () => { if (!drag) hideHover(); });

  plotEl.addEventListener("keydown", (e) => {
    const step = view.span * 0.15;
    if (e.key === "ArrowRight") { panBy(step); setFollowing(false); }
    else if (e.key === "ArrowLeft") { panBy(-step); setFollowing(false); }
    else if (e.key === "+" || e.key === "=") zoomAt(plotEl.clientWidth / 2, 0.7);
    else if (e.key === "-" || e.key === "_") zoomAt(plotEl.clientWidth / 2, 1.4);
    else if (e.key.toLowerCase() === "f") fit();
    else return;
    e.preventDefault();
  });
}

// ---- the time model ------------------------------------------------------

// Wall-clock ms per event, with fallbacks: sessions projected from an old
// message-only log carry `ts: null`, and a log with no timestamps at all falls
// back to one synthetic second per event so the view still has an axis.
function eventTimes(evs) {
  const n = evs.length;
  const raw = new Array(n);
  let firstIdx = -1;
  for (let i = 0; i < n; i++) {
    const t = evs[i].ts ? Date.parse(evs[i].ts) : NaN;
    raw[i] = Number.isFinite(t) ? t : null;
    if (firstIdx < 0 && raw[i] != null) firstIdx = i;
  }
  if (firstIdx < 0) {
    for (let i = 0; i < n; i++) raw[i] = i * 1000;
    return raw;
  }
  for (let i = firstIdx - 1; i >= 0; i--) raw[i] = raw[firstIdx] - (firstIdx - i);
  let prev = raw[firstIdx];
  for (let i = firstIdx; i < n; i++) {
    if (raw[i] == null) raw[i] = prev; else prev = raw[i];
  }
  // The log is append-ordered; clock jitter must never invert it.
  for (let i = 1; i < n; i++) if (raw[i] < raw[i - 1]) raw[i] = raw[i - 1];
  return raw;
}

function buildModel() {
  const evs = store.events;
  const n = evs.length;
  if (!n) { model = emptyModel(); return; }

  const raw = eventTimes(evs);
  const resultAt = new Map();      // tool_call id -> index of its tool_result
  const agentPrev = new Map();     // agent_id -> index of that agent's last event
  const agentEnd = new Map();      // agent_id -> last time seen (for agent_spawned)
  // agent_id -> the definition it was spawned from. Only `agent_spawned`
  // carries it, so every later event from that agent has to look it up here to
  // link back to the definition that decided what it could do.
  const agentDefs = new Map();
  for (let i = 0; i < n; i++) {
    const ev = evs[i];
    if (ev.type === "tool_result" && ev.id) resultAt.set(ev.id, i);
    if (ev.agent_id) agentEnd.set(ev.agent_id, raw[i]);
    if (ev.type === "agent_spawned" && ev.agent_id && ev.definition) {
      agentDefs.set(ev.agent_id, ev.definition);
    }
  }

  const items = new Array(n);
  const bySeq = new Map();
  for (let i = 0; i < n; i++) {
    const ev = evs[i];
    const inner = ev.type === "agent_event" ? (ev.ev || {}) : ev;
    const role = roleOf(ev);
    let t0 = raw[i];
    let t1 = raw[i];
    let inferred = false;

    if (ev.type === "tool_call") {
      const j = ev.id != null ? resultAt.get(ev.id) : undefined;
      const res = j != null ? evs[j] : null;
      if (res) t1 = Math.max(raw[j], t0 + (res.ms || 0));
      else if (i === n - 1) t1 = t0;   // still running; no honest end yet
    } else if (ev.type === "tool_result") {
      t0 = t1 = raw[i];
    } else if (inner.type === "assistant_message") {
      // The record lands when the turn completes, so the model was busy from
      // whatever happened last until now.
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

    const it = {
      i, seq: ev.seq, ev, inner, role,
      lane: LANE_OF[role] ?? 1,
      t0, t1,
      err: !!(inner.is_error || inner.type === "error"),
      shown: true,
      v0: 0, v1: 0,
    };
    items[i] = it;
    bySeq.set(ev.seq, it);
  }

  const tMin = Math.min(raw[0], items[0].t0);
  let tMax = tMin;
  for (const it of items) if (it.t1 > tMax) tMax = it.t1;

  const segs = buildSegments(items, tMin, tMax);
  for (const it of items) {
    // Pin a bar into the segment its end lives in, so nothing is ever drawn
    // straddling a collapsed band.
    const s = segAt(segs, it.t1);
    if (it.t0 < s.r0) it.t0 = s.r0;
    it.v0 = toVirt(segs, it.t0);
    it.v1 = Math.max(it.v0, toVirt(segs, it.t1));
  }

  const lastSeg = segs[segs.length - 1];
  model = { items, bySeq, segs, agentDefs, tMin, tMax, totalV: Math.max(1, lastSeg.v1) };
}

// Merge every bar's [t0,t1] into coverage; whatever is left uncovered and long
// enough becomes a collapsed segment. Collapsing keeps a 90-minute session with
// two 4-minute coffee breaks from squashing its real work into a few pixels.
function buildSegments(items, tMin, tMax) {
  if (!(tMax > tMin)) {
    // A single event, or every event on the same millisecond: give the axis a
    // synthetic second so bars and ticks have somewhere to live.
    return [{ r0: tMin, r1: tMin + 1000, v0: 0, v1: 1000, collapsed: false }];
  }
  const spans = items.map((it) => [it.t0, it.t1]).sort((a, b) => a[0] - b[0]);
  const covered = [];
  for (const [a, b] of spans) {
    const last = covered[covered.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else covered.push([a, b]);
  }
  const thr = Math.max(GAP_MIN_MS, (tMax - tMin) * 0.02);
  const gaps = [];
  if (collapseGaps) {
    for (let i = 1; i < covered.length; i++) {
      const a = covered[i - 1][1], b = covered[i][0];
      if (b - a > thr) gaps.push([a, b]);
    }
  }
  const active = (tMax - tMin) - gaps.reduce((s, g) => s + (g[1] - g[0]), 0);
  // Wide enough that the hatched band reads as a deliberate break rather than
  // a rendering seam, narrow enough that the real work still owns the axis.
  const gapV = Math.max(250, active * 0.035);

  const segs = [];
  let r = tMin, v = 0;
  for (const [a, b] of gaps) {
    if (a > r) { segs.push({ r0: r, r1: a, v0: v, v1: v + (a - r), collapsed: false }); v += a - r; }
    segs.push({ r0: a, r1: b, v0: v, v1: v + gapV, collapsed: true });
    v += gapV; r = b;
  }
  segs.push({ r0: r, r1: Math.max(tMax, r + 1), v0: v, v1: v + Math.max(1, tMax - r), collapsed: false });
  return segs;
}

function segAt(segs, t) {
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid].r0 <= t) lo = mid; else hi = mid - 1;
  }
  return segs[lo];
}

function segAtV(segs, v) {
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid].v0 <= v) lo = mid; else hi = mid - 1;
  }
  return segs[lo];
}

function toVirt(segs, t) {
  const s = segAt(segs, t);
  if (t >= s.r1) return s.v1;
  const dr = s.r1 - s.r0;
  return s.collapsed
    ? s.v0 + (dr ? ((t - s.r0) / dr) * (s.v1 - s.v0) : 0)
    : s.v0 + (t - s.r0);
}

function toReal(segs, v) {
  const s = segAtV(segs, v);
  const dv = s.v1 - s.v0;
  return s.collapsed
    ? s.r0 + (dv ? ((v - s.v0) / dv) * (s.r1 - s.r0) : 0)
    : s.r0 + (v - s.v0);
}

// ---- viewport ------------------------------------------------------------

function plotW() { return Math.max(1, plotEl.clientWidth); }
function xOf(v) { return ((v - view.v0) / view.span) * plotW(); }
function vOf(x) { return view.v0 + (x / plotW()) * view.span; }

function setView(v0, span) {
  const total = model.totalV;
  span = Math.min(Math.max(span, 4), Math.max(total, 4));
  v0 = Math.min(Math.max(v0, -span * 0.02), Math.max(0, total - span) + span * 0.02);
  view = { v0, span };
  invalidate(2);
}

function fit() {
  setView(0, model.totalV);
}

function panBy(dv) { setView(view.v0 + dv, view.span); }

function zoomAt(px, k) {
  const anchor = vOf(px);
  const span = Math.min(Math.max(view.span * k, 4), Math.max(model.totalV, 4));
  setView(anchor - (px / plotW()) * span, span);
}

// Keep `v` comfortably inside the viewport, recentring only when it fell out.
function ensureVisible(v, { center = false } = {}) {
  if (center) { setView(v - view.span / 2, view.span); return; }
  const pad = view.span * 0.08;
  if (v < view.v0 + pad) setView(v - pad, view.span);
  else if (v > view.v0 + view.span - pad) setView(v - view.span + pad, view.span);
}

// ---- painting ------------------------------------------------------------

function invalidate(bits) {
  dirty |= bits;
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    const d = dirty; dirty = 0;
    if (d & 1) { buildModel(); rebuildRows(); }
    if (d & (2 | 1)) paintLanes();
    if (d & (4 | 1)) paintRows();
  });
}

function measure() {
  const cs = getComputedStyle(table);
  rowH = parseFloat(cs.getPropertyValue("--tj-row-h")) || 24;
}

function renderAll() {
  measure();
  buildModel();
  rebuildRows();
  if (playV != null) playV = Math.min(playV, model.totalV);
  paintLanes();
  paintRows();
  if (following) scrollToNewest();
}

function rebuildRows() {
  rows = [];
  for (const it of model.items) {
    it.shown = visible(it.ev);
    if (it.shown) rows.push(it);
  }
}

function newestSeq() {
  return store.events.length ? store.events[store.events.length - 1].seq : null;
}

function paintLanes() {
  const W = plotW();
  const buckets = LANES.map(() => []);
  const seen = new Set();
  hits = [];
  const newest = following ? newestSeq() : null;

  for (const it of model.items) {
    if (!it.shown) continue;
    const x0 = xOf(it.v0);
    const xe = xOf(it.v1);
    if (xe < -LANE_OVERSCAN_PX || x0 > W + LANE_OVERSCAN_PX) continue;
    const w = Math.max(MIN_BAR_PX, xe - x0);
    // Sub-pixel bars of the same role in the same lane are indistinguishable,
    // so only the first one at a given x becomes a node.
    if (w <= MIN_BAR_PX + 0.5) {
      const key = it.lane + "|" + it.role + "|" + (x0 | 0);
      if (seen.has(key)) continue;
      seen.add(key);
    }
    let cls = "tj-bar r-" + it.role;
    if (it.err) cls += " is-err";
    if (it.seq === selectedSeq) cls += " is-sel";
    if (it.seq === newest) cls += " is-newest";
    buckets[it.lane].push(
      `<i class="${cls}" style="left:${x0.toFixed(1)}px;width:${w.toFixed(1)}px"></i>`);
    hits.push({ it, x0, x1: x0 + w, lane: it.lane });
    if (hits.length >= MAX_BARS) break;
  }
  for (let i = 0; i < laneEls.length; i++) laneEls[i].innerHTML = buckets[i].join("");

  paintBands(W);
  paintAxis(W);
  paintPlayhead(W);
}

// Collapsed stretches are hatched and labelled with the real idle time, so the
// axis never silently lies about how much wall clock it skipped. The label is
// allowed to spill past its narrow band — but only where it will not land on
// the previous one.
function paintBands(W) {
  let html = "";
  let lastLabelRight = -1e9;
  for (const s of model.segs) {
    if (!s.collapsed) continue;
    const x0 = xOf(s.v0), x1 = xOf(s.v1);
    if (x1 < -20 || x0 > W + 20) continue;
    const w = Math.max(2, x1 - x0);
    const text = "⋯ " + fmtDur(s.r1 - s.r0) + " idle";
    const est = text.length * 5.6 + 8;
    const mid = x0 + w / 2;
    let label = "";
    if (mid - est / 2 > lastLabelRight + 6 && mid > 4 && mid < W - 4) {
      label = `<b>${esc(text)}</b>`;
      lastLabelRight = mid + est / 2;
    }
    html += `<i class="tj-band" style="left:${x0.toFixed(1)}px;width:${w.toFixed(1)}px">${label}</i>`;
  }
  bandsEl.innerHTML = html;
}

const STEPS = [
  100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
  600000, 900000, 1800000, 3600000, 7200000, 14400000, 43200000, 86400000,
];

// Ticks are generated per visible non-collapsed segment. Deriving the step from
// the raw real span instead would let one four-hour collapsed gap pick a
// two-hour step that no visible tick can ever land on.
function paintAxis(W) {
  const segs = model.segs;
  const vEnd = view.v0 + view.span;
  const vis = [];
  let active = 0;
  for (const s of segs) {
    if (s.v1 < view.v0 || s.v0 > vEnd) continue;
    const r0 = toReal(segs, Math.max(s.v0, view.v0));
    const r1 = toReal(segs, Math.min(s.v1, vEnd));
    vis.push({ s, r0, r1 });
    if (!s.collapsed) active += r1 - r0;
  }
  const target = Math.max(2, Math.floor(W / TICK_TARGET_PX));
  let step = STEPS[STEPS.length - 1];
  for (const st of STEPS) if (Math.max(1, active) / st <= target) { step = st; break; }

  const marks = [];
  for (const { s, r0, r1 } of vis) {
    if (s.collapsed) continue;
    marks.push(r0);                                 // the moment work resumes
    for (let t = Math.ceil(r0 / step) * step, g = 0; t <= r1 && g < 200; t += step, g++) {
      marks.push(t);
    }
  }
  marks.sort((a, b) => a - b);

  let html = "";
  let lastX = -1e9;
  for (const t of marks) {
    const x = xOf(toVirt(segs, t));
    if (x < 0 || x > W) continue;
    if (x - lastX < 54) continue;
    lastX = x;
    html += `<i class="tj-tick" style="left:${x.toFixed(1)}px"><b>${esc(fmtRel(t - model.tMin))}</b></i>`;
  }
  axisEl.innerHTML = html;
}

function paintPlayhead(W) {
  if (playV == null) { playEl.classList.add("hidden"); return; }
  const x = xOf(playV);
  if (x < -2 || x > W + 2) { playEl.classList.add("hidden"); return; }
  playEl.classList.remove("hidden");
  playEl.style.left = x.toFixed(1) + "px";
}

// ---- the windowed table --------------------------------------------------

function rowHTML(it, i) {
  const ms = it.inner.type === "tool_result" && it.inner.ms ? fmtMs(it.inner.ms) : "";
  let res = "";
  // A subagent's call arrives wrapped, so read the inner event: keying on
  // `it.ev.type` left every delegated call in the log without its result.
  if (it.inner.type === "tool_call" && it.inner.id) {
    const r = toolResultFor(it.inner.id, it.ev.agent_id);
    if (r) res = `<span class="tj-res">→ ${esc(oneLine(r.content, 90))}</span>`;
  }
  return `<div class="tj-row${it.err ? " is-error" : ""}${it.seq === selectedSeq ? " selected" : ""}"
    data-seq="${it.seq}" role="button" tabindex="0" style="top:${i * rowH}px">
    <span class="seq">${it.seq}</span>
    <span class="chip chip-${it.role}">${it.role}</span>
    <span class="preview">${esc(previewOf(it.ev))}</span>${res}
    ${configLinkHtml(it.ev, it.inner)}
    <span class="t-ms">${ms}</span></div>`;
}

function paintRows() {
  spacerEl.style.height = rows.length * rowH + "px";
  const h = table.clientHeight || 400;
  const top = table.scrollTop;
  const first = Math.max(0, Math.floor(top / rowH) - ROW_OVERSCAN);
  const last = Math.min(rows.length, Math.ceil((top + h) / rowH) + ROW_OVERSCAN);
  let html = "";
  for (let i = first; i < last; i++) html += rowHTML(rows[i], i);
  rowsEl.innerHTML = html;
}

// ---- live follow ---------------------------------------------------------

function atBottom() {
  return table.scrollHeight - table.scrollTop - table.clientHeight < 24;
}

function scrollToNewest() {
  // The scroll event lands a tick later; a short window is what tells our own
  // scroll apart from the user's.
  autoScrollUntil = Date.now() + 150;
  table.scrollTop = table.scrollHeight;
  const last = model.items[model.items.length - 1];
  if (last) {
    playV = last.v1;
    ensureVisible(last.v1);
    invalidate(2);
  }
  invalidate(4);
}

function setFollowing(on, { jump = false } = {}) {
  following = on;
  if (followBtn) {
    followBtn.textContent = on ? "⏸ Follow" : "▶ Follow";
    followBtn.classList.toggle("paused", !on);
    followBtn.setAttribute("aria-pressed", on ? "true" : "false");
    followBtn.title = on
      ? "Following the newest event — click to pause"
      : "Paused — click to jump to the newest event and resume";
  }
  if (on && jump) {
    scrollToNewest();
    const seq = newestSeq();
    if (seq != null && !detail.classList.contains("hidden")) {
      selectSeq(seq, { keepFollow: true });
    }
  }
  invalidate(2);
}

function appendEvent() {
  invalidate(1);
  if (!following) return;
  requestAnimationFrame(() => {
    scrollToNewest();
    // The inspector tracks the live edge only while it is already open.
    const seq = newestSeq();
    if (seq != null && !detail.classList.contains("hidden")) {
      selectSeq(seq, { keepFollow: true });
    }
  });
}

// ---- interaction ---------------------------------------------------------

function hitAt(x, laneIdx) {
  let best = null, bestD = Infinity;
  for (const h of hits) {
    if (h.lane !== laneIdx) continue;
    const d = x < h.x0 ? h.x0 - x : x > h.x1 ? x - h.x1 : 0;
    if (d < bestD) { bestD = d; best = h; }
    if (d === 0) break;
  }
  if (best && bestD <= HIT_SLOP_PX * 5) return best;
  // Nothing in this lane: fall back to the nearest bar anywhere.
  best = null; bestD = Infinity;
  for (const h of hits) {
    const d = x < h.x0 ? h.x0 - x : x > h.x1 ? x - h.x1 : 0;
    if (d < bestD) { bestD = d; best = h; }
  }
  return bestD <= HIT_SLOP_PX * 5 ? best : null;
}

function laneAt(y) {
  const lane = laneEls[0];
  const lh = lane ? lane.offsetHeight || 1 : 1;
  return Math.max(0, Math.min(LANES.length - 1, Math.floor(y / lh)));
}

function onHover(e) {
  const rect = plotEl.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;
  cursorEl.classList.remove("hidden");
  cursorEl.style.left = x.toFixed(1) + "px";
  const h = hitAt(x, laneAt(y));
  if (!h) { hoverEl.classList.add("hidden"); return; }
  showHoverCard(h.it, x, rect);
}

function showHoverCard(it, x, rect) {
  const kind = it.inner.type || it.ev.type;
  const dur = it.t1 - it.t0;
  const target = configTarget(it.ev, it.inner);
  const ms = it.inner.type === "tool_result" && it.inner.ms != null ? it.inner.ms : dur;
  hoverEl.innerHTML =
    `<div class="hc-head"><span class="chip chip-${it.role}">${it.role}</span>
       <span class="hc-kind">${esc(kind)}</span><span class="hc-seq">#${it.seq}</span></div>
     <div class="hc-prev">${esc(oneLine(previewOf(it.ev), 150))}</div>
     <div class="hc-time"><span>${esc(clock(it.t0))}</span>
       <span class="hc-arrow">→</span>
       <span>${esc(clock(it.t1))}</span>
       <span class="hc-dur">${esc(ms ? fmtMs(Math.round(ms)) : "0 ms")}</span></div>
     <div class="hc-off">at ${esc(fmtRel(it.t0 - model.tMin))}${dur ? " · spans " + esc(fmtDur(dur)) : ""}${
       target ? " · governed by " + esc(target.label) : ""}</div>`;
  hoverEl.classList.remove("hidden");
  const w = hoverEl.offsetWidth || 240;
  const host = timelineEl.getBoundingClientRect();
  let left = rect.left - host.left + x + 14;
  if (left + w > host.width - 6) left = Math.max(4, rect.left - host.left + x - w - 14);
  hoverEl.style.left = left.toFixed(1) + "px";
}

function hideHover() {
  cursorEl.classList.add("hidden");
  hoverEl.classList.add("hidden");
}

function onPlotClick(e) {
  const rect = plotEl.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const h = hitAt(x, laneAt(e.clientY - rect.top));
  playV = vOf(x);
  if (h) selectSeq(h.it.seq, { scroll: true, keepPlayhead: true });
  else invalidate(2);
}

export function selectSeq(seq, { scroll = false, center = false, keepFollow = false, keepPlayhead = false } = {}) {
  // Picking an event by hand is the other way to say "let me read".
  if (!keepFollow && following) setFollowing(false);
  selectedSeq = seq;
  const it = model.bySeq.get(seq);
  if (it) {
    if (!keepPlayhead) { playV = it.v0; ensureVisible(it.v0, { center }); }
    const idx = rows.indexOf(it);
    if (idx >= 0 && (scroll || center)) {
      autoScrollUntil = Date.now() + 150;
      table.scrollTop = Math.max(0, idx * rowH - table.clientHeight / 2);
    }
  }
  detail.classList.remove("hidden");
  renderDetail();
  invalidate(6);
}

// ---- formatting ----------------------------------------------------------

function clock(ms) {
  const d = new Date(ms);
  return isNaN(d) ? "–" : d.toLocaleTimeString();
}

function fmtRel(ms) {
  const v = Math.max(0, ms);
  if (v < 10000) return "+" + (v / 1000).toFixed(1) + "s";
  const tot = Math.round(v / 1000);
  const h = Math.floor(tot / 3600), m = Math.floor((tot % 3600) / 60), s = tot % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h ? `+${h}:${p(m)}:${p(s)}` : `+${m}:${p(s)}`;
}

function fmtDur(ms) {
  if (ms < 1000) return Math.round(ms) + " ms";
  if (ms < 60000) return (ms / 1000).toFixed(1) + " s";
  const tot = Math.round(ms / 1000);
  const h = Math.floor(tot / 3600), m = Math.floor((tot % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${tot % 60}s`;
}

// ---- inspector (unchanged) -----------------------------------------------

function renderDetail() {
  const ev = store.events.find((e) => e.seq === selectedSeq);
  if (!ev) { detailBody.innerHTML = ""; return; }
  const role = roleOf(ev);
  const inner = ev.type === "agent_event" ? ev.ev || {} : ev;
  detailTitle.innerHTML = `<span class="chip chip-${role}" style="padding:1px 8px;border-radius:4px;color:var(--bg)">${role}</span>
    &nbsp;#${ev.seq} · ${esc(inner.type || ev.type)}`;

  if (detailTab === "summary") {
    const target = configTarget(ev, inner);
    const rowsKv = [
      ["Type", inner.type || ev.type],
      ["Turn", ev.turn ?? "–"],
      ["Sequence", ev.seq],
      ev.agent_id ? ["Agent", ev.agent_id] : null,
      inner.name ? ["Tool", inner.name] : null,
      inner.finish_reason ? ["Finish", inner.finish_reason] : null,
      inner.is_error != null ? ["Status", inner.is_error ? "error" : "ok"] : null,
      // The last row is the one that turns a record into something you can act
      // on: the page that decided this was allowed to happen.
      target ? ["Governed by", target.label, target.href] : null,
    ].filter(Boolean);
    const kv = rowsKv.map(([k, v, href]) => `<div class="k">${k}</div><div class="v">${
      href ? `<a class="k-link" href="${esc(href)}"
        title="Open it in configuration">${esc(String(v))} ↗</a>`
        : esc(String(v))}</div>`).join("");
    const text = inner.text || inner.content || inner.arguments || inner.plan || inner.message || "";
    detailBody.innerHTML = `<div class="kv">${kv}</div>` +
      (text ? `<pre>${esc(String(text).slice(0, 20000))}</pre>` : "");
  } else if (detailTab === "payload") {
    detailBody.innerHTML = `<pre>${esc(JSON.stringify(ev, null, 2).slice(0, 60000))}</pre>`;
  } else if (detailTab === "result") {
    let result = null;
    if (inner.type === "tool_call") result = toolResultFor(inner.id, ev.agent_id);
    else if (inner.type === "tool_result") result = inner;
    detailBody.innerHTML = result
      ? `<pre>${esc(String(result.content).slice(0, 60000))}</pre>`
      : `<div class="kv"><div class="k">Result</div><div class="v">— not applicable —</div></div>`;
  } else if (detailTab === "timing") {
    const it = model.bySeq.get(selectedSeq);
    const rowsKv = [
      ["Started", fmtTime(ev.ts) || "–"],
      inner.ms != null ? ["Duration", fmtMs(inner.ms)] : null,
      it ? ["Offset", fmtRel(it.t0 - model.tMin)] : null,
      it && it.t1 > it.t0 ? ["Span", fmtDur(it.t1 - it.t0)] : null,
      ["Source", ev.agent_id ? `subagent ${ev.agent_id}` : "main agent"],
    ].filter(Boolean);
    detailBody.innerHTML = `<div class="kv">${rowsKv.map(([k, v]) =>
      `<div class="k">${k}</div><div class="v">${esc(String(v))}</div>`).join("")}</div>`;
  }
}

function exportLog() {
  const blob = new Blob([JSON.stringify(store.events, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `quickcode-${store.convId || "session"}-trajectory.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}
