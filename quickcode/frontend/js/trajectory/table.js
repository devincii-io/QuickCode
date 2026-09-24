// The event table under the lanes: one absolutely positioned row per visible
// event, windowed to the scroll viewport. Rows are keyed by event, so a row
// that stays on screen keeps its node (and its keyboard focus) while the
// reader scrolls; rows that leave are recycled for the ones that arrive.

import { fmtMs, oneLine } from "../util.js";
import { previewOf } from "./roles.js";
import { rowRange } from "./windowing.js";

// Events never change once logged, and oneLine walks the whole text — a
// system prompt is tens of kilobytes — so each preview is computed once.
const previews = new WeakMap();
function preview(ev) {
  let p = previews.get(ev);
  if (p === undefined) { p = previewOf(ev); previews.set(ev, p); }
  return p;
}

function span(cls) {
  const n = document.createElement("span");
  n.className = cls;
  return n;
}

function makeRow() {
  const row = document.createElement("div");
  row.className = "tj-row";
  row.setAttribute("role", "button");
  row.tabIndex = 0;
  const link = document.createElement("a");
  link.className = "t-ms k-link tj-cfg";
  row.append(span("seq"), span("chip"), span("preview"), span("tj-res"), link, span("t-ms tj-ms"));
  return row;
}

function fill(row, it, target) {
  const [seq, chip, prev, res, link, ms] = row.children;
  row.dataset.seq = String(it.seq);
  row.classList.toggle("is-error", it.err);
  seq.textContent = String(it.seq);
  chip.className = `chip chip-${it.role}`;
  chip.textContent = it.role;
  prev.textContent = preview(it.ev);
  // A subagent's call arrives wrapped; the model already paired it with the
  // result from its own agent's stream.
  const r = it.inner.type === "tool_call" ? it.result : null;
  res.textContent = r ? "→ " + oneLine(r.content, 90) : "";
  res.classList.toggle("hidden", !r);
  if (target) {
    link.href = target.href;
    link.textContent = `${target.label} ↗`;
    link.title = `Open ${target.label} in configuration — this is what governs it`;
  }
  link.classList.toggle("hidden", !target);
  const took = it.inner.ms ?? r?.ms;
  ms.textContent = took ? fmtMs(took) : "";
}

export function createTable({ spacerEl, rowsEl }) {
  const live = new Map();     // seq -> row node on screen
  const free = [];

  /** Size the scroll area for `count` rows, before anything reads its end. */
  function size(count, rowH) {
    const h = count * rowH + "px";
    if (spacerEl.style.height !== h) spacerEl.style.height = h;
  }

  function paint({ rows, rowH, scrollTop, viewH, selectedSeq, targetOf }) {
    size(rows.length, rowH);
    const { first, last } = rowRange(scrollTop, viewH, rowH, rows.length);
    const want = new Set();
    for (let i = first; i < last; i++) want.add(rows[i].seq);
    for (const [seq, node] of live) {
      if (want.has(seq)) continue;
      live.delete(seq);
      node.hidden = true;
      free.push(node);
    }
    for (let i = first; i < last; i++) {
      const it = rows[i];
      let node = live.get(it.seq);
      if (!node) {
        node = free.pop();
        if (!node) { node = makeRow(); rowsEl.appendChild(node); }
        node.hidden = false;
        live.set(it.seq, node);
      }
      // Identity, not seq: a new conversation numbers from 1 again.
      if (node._ev !== it.ev || node._res !== it.result) {
        fill(node, it, targetOf(it));
        node._ev = it.ev;
        node._res = it.result;
      }
      node.classList.toggle("selected", it.seq === selectedSeq);
      const top = i * rowH + "px";
      if (node.style.top !== top) node.style.top = top;
    }
  }

  /** The row node showing `seq`, if it is on screen. */
  function rowFor(seq) { return live.get(seq) || null; }

  return { paint, size, rowFor };
}
