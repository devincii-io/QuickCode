// Windowing: which rows and which bars the current viewport actually needs,
// and what the pointer is over. Pure, so a 10k-event session can be checked
// without a browser: nothing here touches the DOM or reads layout.

export const ROW_OVERSCAN = 8;
export const LANE_OVERSCAN_PX = 240;
export const MIN_BAR_PX = 3;        // a zero-duration event still needs to be clickable
export const MAX_BARS = 2400;       // backstop; dedup normally keeps us far below
export const HIT_SLOP_PX = 25;
const TRACK_SLOP_PX = 6;

/** The half-open row range [first, last) to paint for a scroll position. */
export function rowRange(scrollTop, viewH, rowH, count, overscan = ROW_OVERSCAN) {
  const first = Math.min(count, Math.max(0, Math.floor(scrollTop / rowH) - overscan));
  const last = Math.min(count, Math.ceil((scrollTop + viewH) / rowH) + overscan);
  return { first, last: Math.max(first, last) };
}

/** The scrollTop that shows the newest row at the bottom. */
export function bottomTop(count, rowH, viewH) {
  return Math.max(0, count * rowH - viewH);
}

/** Vertical placement of a bar on track `k` of a lane split into `n` tracks. */
export function trackBox(k, n, laneH) {
  const pad = n === 1 ? 4 : 2;
  const h = (laneH - pad * 2) / n;
  return { top: pad + k * h, height: Math.max(2, h - (n > 1 ? 1 : 0)) };
}

/** Which lane and track a y offset inside the plot falls on. */
export function laneTrackAt(y, laneH, tracks) {
  const lane = Math.max(0, Math.min(tracks.length - 1, Math.floor(y / laneH)));
  const n = tracks[lane] || 1;
  const pad = n === 1 ? 4 : 2;
  const within = y - lane * laneH - pad;
  const track = Math.max(0, Math.min(n - 1, Math.floor(within / ((laneH - pad * 2) / n))));
  return { lane, track };
}

/**
 * Cull the model to what the viewport shows and decide what to draw.
 *
 * Returns `hits` (one per distinguishable bar, for the pointer) and `bars`
 * (what becomes a node). Sub-pixel bars of the same role on the same track are
 * indistinguishable, so only the first at a given pixel is hittable, and
 * touching ones merge into a single run — which is what keeps a fitted view of
 * ten thousand events at a few hundred nodes. A bar that carries state
 * (selected, newest, running, failed) is never merged, and draws last so it
 * is on top.
 */
export function cullBars(items, { view, W, laneCount, selectedSeq = null, newestSeq = null,
  overscan = LANE_OVERSCAN_PX, maxBars = MAX_BARS }) {
  const k = W / view.span;
  const v0 = view.v0;
  const hits = [];
  const plain = [];
  const marked = [];
  const seen = new Set();
  const runs = new Map();
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (!it.shown) continue;
    const x0 = (it.v0 - v0) * k;
    const xe = (it.v1 - v0) * k;
    if (xe < -overscan || x0 > W + overscan) continue;
    const w = Math.max(MIN_BAR_PX, xe - x0);
    const sel = it.seq === selectedSeq;
    const newest = it.seq === newestSeq;
    const special = sel || newest || it.running || it.err;
    if (w <= MIN_BAR_PX + 0.5 && !special) {
      const key = it.lane + "|" + it.track + "|" + it.role + "|" + (x0 | 0);
      if (seen.has(key)) {
        extendRun(runs, it, x0, w, plain);
        continue;
      }
      seen.add(key);
    }
    hits.push({ it, x0, x1: x0 + w, lane: it.lane, track: it.track });
    if (special) marked.push({ it, x0, w, sel, newest });
    else extendRun(runs, it, x0, w, plain);
    if (hits.length >= maxBars) break;
  }
  const bars = [];
  for (let l = 0; l < laneCount; l++) bars.push([]);
  // Clipped to just past the edges: zoomed deep into a long bar, its true
  // width is millions of pixels, and a box that size is what the compositor
  // then has to paint.
  // Bars in the overscan stay hittable but are not drawn.
  const draw = (b) => {
    if (b.x1 < -EDGE_PX || b.x0 > W + EDGE_PX) return;
    b.x0 = Math.max(b.x0, -EDGE_PX);
    b.x1 = Math.max(b.x0 + MIN_BAR_PX, Math.min(b.x1, W + EDGE_PX));
    bars[b.lane].push(b);
  };
  for (const b of plain) draw(b);
  // Selected last of all: it is the one the reader is looking at.
  marked.sort((a, b) => (a.sel ? 1 : 0) - (b.sel ? 1 : 0));
  for (const m of marked) {
    draw({
      lane: m.it.lane, track: m.it.track, role: m.it.role, x0: m.x0, x1: m.x0 + m.w,
      sel: m.sel, newest: m.newest, running: m.it.running, err: m.it.err,
    });
  }
  return { hits, bars };
}

const EDGE_PX = 16;

function extendRun(runs, it, x0, w, out) {
  const key = it.lane + "|" + it.track + "|" + it.role;
  const run = runs.get(key);
  const x1 = x0 + w;
  if (run && x0 >= run.x0 - 0.5 && x0 <= run.x1 + 0.5) {
    if (x1 > run.x1) run.x1 = x1;
    return;
  }
  const next = { lane: it.lane, track: it.track, role: it.role, x0, x1,
    sel: false, newest: false, running: false, err: false };
  runs.set(key, next);
  out.push(next);
}

function nearest(hits, x, slop, accept) {
  let best = null, bestD = Infinity, bestW = Infinity;
  for (const h of hits) {
    if (!accept(h)) continue;
    const d = x < h.x0 ? h.x0 - x : x > h.x1 ? x - h.x1 : 0;
    const w = h.x1 - h.x0;
    // Inside several bars at once (a result over its call, a subagent's call
    // over its lifetime), the narrowest is the one the pointer means.
    if (d < bestD || (d === bestD && w < bestW)) { best = h; bestD = d; bestW = w; }
  }
  return best && bestD <= slop ? best : null;
}

/** The bar the pointer means: one right there on its own track first, then
 *  the nearest in its lane, then anywhere near — so a lane with nothing under
 *  the pointer still answers. */
export function hitTest(hits, x, { lane, track }) {
  return nearest(hits, x, TRACK_SLOP_PX, (h) => h.lane === lane && h.track === track)
    || nearest(hits, x, HIT_SLOP_PX, (h) => h.lane === lane)
    || nearest(hits, x, HIT_SLOP_PX, () => true);
}
