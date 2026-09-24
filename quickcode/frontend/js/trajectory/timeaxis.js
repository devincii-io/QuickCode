// The time axis: wall-clock ms per event, idle gaps collapsed into segments,
// the real <-> virtual mapping, the viewport, and where the tick labels go.
//
// "Virtual" ms are the axis the viewport pans over: equal to real ms inside an
// active segment, compressed inside a collapsed one. Everything here is pure.

import { clockLabel, dayOf } from "./format.js";

// An idle stretch longer than this (and than 2% of the session) is collapsed.
export const GAP_MIN_MS = 15000;
export const MIN_SPAN = 4;
// Clock pills are wider than bare offsets, so ticks need this much room each.
export const TICK_TARGET_PX = 118;

export const STEPS = [
  100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
  600000, 900000, 1800000, 3600000, 7200000, 14400000, 43200000, 86400000,
];

// Events are immutable once logged, and a live session rebuilds its model on
// every append: parsing ten thousand ISO strings each time was the largest
// single cost of that rebuild.
const parsed = new WeakMap();

function tsOf(ev) {
  let t = parsed.get(ev);
  if (t === undefined) {
    const p = ev.ts ? Date.parse(ev.ts) : NaN;
    t = Number.isFinite(p) ? p : null;
    parsed.set(ev, t);
  }
  return t;
}

/** Wall-clock ms per event. Sessions projected from an old message-only log
 *  carry `ts: null`, and a log with no timestamps at all falls back to one
 *  synthetic second per event so the view still has an axis. */
export function eventTimes(evs) {
  const n = evs.length;
  const raw = new Array(n);
  let firstIdx = -1;
  for (let i = 0; i < n; i++) {
    raw[i] = tsOf(evs[i]);
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

/** Merge the shown spans into coverage; whatever is left uncovered and long
 *  enough becomes a collapsed segment. Collapsing keeps a 90-minute session
 *  with two coffee breaks from squashing its real work into a few pixels.
 *  Coverage comes only from what the filters left on screen: filtering to TOOL
 *  should collapse the minutes where only the model was talking. */
export function buildSegments(spans, tMin, tMax, collapse = true) {
  if (!(tMax > tMin)) {
    // One event, or all on the same millisecond: a synthetic second of axis.
    return [{ i: 0, r0: tMin, r1: tMin + 1000, v0: 0, v1: 1000, collapsed: false }];
  }
  const sorted = spans.slice().sort((a, b) => a[0] - b[0]);
  const covered = [];
  for (const [a, b] of sorted) {
    const last = covered[covered.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else covered.push([a, b]);
  }
  // The axis spans session start → end, so an idle head or tail is a gap like
  // any other; zero-length sentinels find them without a second code path.
  if (!covered.length) covered.push([tMin, tMin]);
  if (covered[0][0] > tMin) covered.unshift([tMin, tMin]);
  if (covered[covered.length - 1][1] < tMax) covered.push([tMax, tMax]);
  const thr = Math.max(GAP_MIN_MS, (tMax - tMin) * 0.02);
  const gaps = [];
  if (collapse) {
    for (let i = 1; i < covered.length; i++) {
      const a = covered[i - 1][1], b = covered[i][0];
      if (b - a > thr) gaps.push([a, b]);
    }
  }
  const active = (tMax - tMin) - gaps.reduce((s, g) => s + (g[1] - g[0]), 0);
  // Wide enough that the hatched band reads as a deliberate break, narrow
  // enough that the real work still owns the axis.
  const gapV = Math.max(250, active * 0.035);

  const segs = [];
  let r = tMin, v = 0;
  for (const [a, b] of gaps) {
    if (a > r) { segs.push({ r0: r, r1: a, v0: v, v1: v + (a - r), collapsed: false }); v += a - r; }
    segs.push({ r0: a, r1: b, v0: v, v1: v + gapV, collapsed: true });
    v += gapV; r = b;
  }
  segs.push({ r0: r, r1: Math.max(tMax, r + 1), v0: v, v1: v + Math.max(1, tMax - r), collapsed: false });
  segs.forEach((s, i) => { s.i = i; });
  return segs;
}

export function segAt(segs, t) {
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid].r0 <= t) lo = mid; else hi = mid - 1;
  }
  return segs[lo];
}

export function segAtV(segs, v) {
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (segs[mid].v0 <= v) lo = mid; else hi = mid - 1;
  }
  return segs[lo];
}

export function toVirt(segs, t) {
  const s = segAt(segs, t);
  if (t >= s.r1) return s.v1;
  const dr = s.r1 - s.r0;
  return s.collapsed
    ? s.v0 + (dr ? ((t - s.r0) / dr) * (s.v1 - s.v0) : 0)
    : s.v0 + (t - s.r0);
}

export function toReal(segs, v) {
  const s = segAtV(segs, v);
  const dv = s.v1 - s.v0;
  return s.collapsed
    ? s.r0 + (dv ? ((v - s.v0) / dv) * (s.r1 - s.r0) : 0)
    : s.r0 + (v - s.v0);
}

/** The segment a bar is drawn in. A bar that ends exactly where a gap begins —
 *  a tool call, then twenty idle minutes — belongs to the segment it ran in;
 *  segAt alone picks the gap and shrinks the call to a dot inside the band. */
export function homeSegment(segs, t1) {
  const at = segAt(segs, t1);
  return at.i > 0 && at.r0 === t1 ? segs[at.i - 1] : at;
}

// ---- viewport ------------------------------------------------------------

/** Clamp a viewport to the session, keeping a 2% margin either side. */
export function clampView(v0, span, total) {
  span = Math.min(Math.max(span, MIN_SPAN), Math.max(total, MIN_SPAN));
  v0 = Math.min(Math.max(v0, -span * 0.02), Math.max(0, total - span) + span * 0.02);
  return { v0, span };
}

/** Zoom by `k` about the pixel `px`, which keeps the instant under it fixed. */
export function zoomView(view, px, W, k, total) {
  const anchor = view.v0 + (px / W) * view.span;
  const span = Math.min(Math.max(view.span * k, MIN_SPAN), Math.max(total, MIN_SPAN));
  return clampView(anchor - (px / W) * span, span, total);
}

/** Whether the viewport shows the whole session. Relative, not exact: the
 *  clamp's margin would otherwise read as "zoomed in" after any nudge. */
export function isFitted(view, total) {
  const tol = Math.max(4, total * 0.035);
  return view.v0 <= tol && view.v0 + view.span >= total - tol;
}

/** Keep `v` comfortably inside the viewport, moving only when it fell out. */
export function revealView(view, v, total, { center = false } = {}) {
  if (center) return clampView(v - view.span / 2, view.span, total);
  const pad = view.span * 0.08;
  if (v < view.v0 + pad) return clampView(v - pad, view.span, total);
  if (v > view.v0 + view.span - pad) return clampView(v - view.span + pad, view.span, total);
  return view;
}

// ---- ticks ---------------------------------------------------------------

/** The smallest step that puts no more than `target` ticks across `activeMs`. */
export function pickStep(activeMs, W) {
  const target = Math.max(2, Math.floor(W / TICK_TARGET_PX));
  for (const st of STEPS) if (Math.max(1, activeMs) / st <= target) return st;
  return STEPS[STEPS.length - 1];
}

/** Where the axis labels go and what they say.
 *
 *  Ticks are generated per visible non-collapsed segment: deriving the step
 *  from the raw span would let one four-hour gap pick a two-hour step no
 *  visible tick can land on. Pills are claimed in order of what the reader
 *  loses least by not seeing — a round number can be inferred from its
 *  neighbours; the live edge and the clock on the far side of a collapsed gap
 *  cannot — and collide on the box they will occupy. */
export function planTicks({ segs, view, W, live, tMin, tMax }) {
  const xOf = (v) => ((v - view.v0) / view.span) * W;
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
  const step = pickStep(active, W);
  const resumes = [];
  const grid = [];
  for (const { s, r0, r1 } of vis) {
    if (s.collapsed) continue;
    // A segment merely clipped by the viewport edge gets no resume mark:
    // there the clock is a scroll position, not something that happened.
    if (s.v0 >= view.v0) resumes.push(r0);
    for (let t = Math.ceil(r0 / step) * step, g = 0; t <= r1 && g < 200; t += step, g++) {
      grid.push(t);
    }
  }
  // Sessions rarely cross midnight, and the ones that do must not read as one
  // afternoon: the date goes only on the pills where the day turns over.
  const crossesDay = vis.length > 0 && dayOf(vis[0].r0) !== dayOf(vis[vis.length - 1].r1);
  const placed = [];
  const chosen = [];
  const take = (t, kind) => {
    const x = xOf(toVirt(segs, t));
    if (x < 0 || x > W) return;
    const prec = kind === "grid" ? step : Math.min(step, 1000);
    const text = (kind === "now" ? "now " : "") + clockLabel(t, prec);
    // Estimated, not measured: a layout per tick per frame is what this avoids.
    const w = (text.length + (crossesDay ? 7 : 0)) * 5.9 + 16;
    const lo = x < 36 ? x : x > W - 36 ? x - w : x - w / 2;
    const box = [lo - 4, lo + w + 4];
    for (const [a, b] of placed) if (box[0] < b && a < box[1]) return;
    placed.push(box);
    chosen.push({ t, x, kind, text });
  };
  if (live) take(tMax, "now");
  for (const t of resumes) take(t, "resume");
  for (const t of grid) take(t, "grid");
  chosen.sort((a, b) => a.t - b.t);

  let lastDay = crossesDay ? "" : dayOf(tMin);
  for (const c of chosen) {
    const day = dayOf(c.t);
    if (c.kind !== "now" && day !== lastDay) c.text = dayOf(c.t, true) + " " + c.text;
    lastDay = day;
    // Centred on the tick, except at the viewport ends, which have nowhere
    // to spill into and anchor inwards instead.
    c.anchor = c.x < 36 ? "start" : c.x > W - 36 ? "end" : "center";
  }
  return { step, ticks: chosen };
}
