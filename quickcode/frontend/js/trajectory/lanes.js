// Painting the timeline strip: gutter labels, duration bars, collapsed-gap
// bands, the clock axis, the playhead and the crosshair. Every node is pooled
// and every label is set as text; nothing here parses markup.

import { fmtMs } from "../util.js";
import { nodePool } from "./pool.js";
import { LANES } from "./roles.js";
import { trackBox } from "./windowing.js";

function make(tag, cls) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  return n;
}

// A pill with its label in a <b>, the shape both ticks and bands use.
function pill(cls) {
  const n = make("i", cls);
  n.appendChild(make("b"));
  return n;
}

export function createLanes({ plotEl, gutterEl, bandsEl, axisEl, playEl, cursorEl }) {
  gutterEl.replaceChildren(...LANES.map((l) => {
    const g = make("div", "tj-glabel");
    g.dataset.lane = l.key;
    const s = make("span");
    s.textContent = l.label;
    g.appendChild(s);
    return g;
  }));
  plotEl.querySelectorAll(".tj-lane").forEach((n) => n.remove());
  const laneEls = LANES.map((l) => {
    const n = make("div", "tj-lane");
    n.dataset.lane = l.key;
    plotEl.insertBefore(n, bandsEl);
    return n;
  });
  const barPools = laneEls.map((el) => nodePool(el, () => make("i")));
  const bandPool = nodePool(bandsEl, () => pill("tj-band"));
  const tickPool = nodePool(axisEl, () => pill("tj-tick"));
  // The crosshair's own clock, drawn on the axis above wherever the pointer is.
  const cursorPill = pill("tj-tick is-cursor");
  cursorPill.hidden = true;
  axisEl.appendChild(cursorPill);

  function paintBars(bars, tracks, laneH) {
    for (let l = 0; l < laneEls.length; l++) {
      const pool = barPools[l];
      const n = tracks[l] || 1;
      pool.begin();
      for (const b of bars[l]) {
        const node = pool.next();
        let cls = "tj-bar r-" + b.role;
        if (b.running) cls += " is-run";
        if (b.err) cls += " is-err";
        if (b.sel) cls += " is-sel";
        if (b.newest) cls += " is-newest";
        if (node.className !== cls) node.className = cls;
        const box = trackBox(Math.min(b.track, n - 1), n, laneH);
        node.style.cssText = `left:${b.x0.toFixed(1)}px;width:${(b.x1 - b.x0).toFixed(1)}px;`
          + `top:${box.top.toFixed(1)}px;height:${box.height.toFixed(1)}px`;
      }
      pool.end();
    }
  }

  // Collapsed stretches are hatched and labelled with the real idle time, so
  // the axis never silently lies about how much clock it skipped. A label may
  // spill past its narrow band, but never onto the previous one.
  function paintBands(segs, xOf, W) {
    bandPool.begin();
    let lastRight = -1e9;
    for (const s of segs) {
      if (!s.collapsed) continue;
      const rx0 = xOf(s.v0), rx1 = xOf(s.v1);
      if (rx1 < -20 || rx0 > W + 20) continue;
      // Clipped like the bars, with the hatching shifted to match, so a band
      // zoomed to a million pixels wide still paints only what is on screen.
      const x0 = Math.max(rx0, -20), x1 = Math.min(rx1, W + 20);
      const w = Math.max(2, x1 - x0);
      const node = bandPool.next();
      node.style.cssText = `left:${x0.toFixed(1)}px;width:${w.toFixed(1)}px;`
        + `background-position:${(rx0 - x0).toFixed(1)}px 0`;
      const text = "⋯ " + fmtMs(s.r1 - s.r0) + " idle";
      const est = text.length * 5.6 + 8;
      const mid = x0 + w / 2;
      const label = node.firstChild;
      if (mid - est / 2 > lastRight + 6 && mid > 4 && mid < W - 4) {
        label.textContent = text;
        label.hidden = false;
        lastRight = mid + est / 2;
      } else {
        label.hidden = true;
      }
    }
    bandPool.end();
  }

  function paintAxis(ticks) {
    tickPool.begin();
    for (const t of ticks) {
      const node = tickPool.next();
      const cls = "tj-tick" + (t.anchor === "start" ? " a-s" : t.anchor === "end" ? " a-e" : "")
        + (t.kind === "now" ? " is-now" : "");
      if (node.className !== cls) node.className = cls;
      node.style.left = t.x.toFixed(1) + "px";
      if (node.firstChild.textContent !== t.text) node.firstChild.textContent = t.text;
    }
    tickPool.end();
  }

  function paintPlayhead(x, W) {
    if (x == null || x < -2 || x > W + 2) { playEl.classList.add("hidden"); return; }
    playEl.classList.remove("hidden");
    playEl.style.left = x.toFixed(1) + "px";
  }

  function showCursor(x, W, label) {
    cursorEl.classList.remove("hidden");
    cursorEl.style.left = x.toFixed(1) + "px";
    if (label == null) { cursorPill.hidden = true; return; }
    cursorPill.hidden = false;
    cursorPill.className = "tj-tick is-cursor" + (x < 36 ? " a-s" : x > W - 36 ? " a-e" : "");
    cursorPill.style.left = x.toFixed(1) + "px";
    cursorPill.firstChild.textContent = label;
  }

  function hideCursor() {
    cursorEl.classList.add("hidden");
    cursorPill.hidden = true;
  }

  return { laneEls, paintBars, paintBands, paintAxis, paintPlayhead, showCursor, hideCursor };
}
