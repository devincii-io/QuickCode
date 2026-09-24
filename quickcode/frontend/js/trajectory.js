// Trajectory view: a wall-clock timeline over the append-only event log.
//
// Four lanes (Input / Model / Tools / Agents) of duration bars sit on a
// clock-time axis with collapsible idle gaps, a hover crosshair with a rich
// card, and a persistent playhead; parallel work stacks into tracks inside its
// lane. While a turn is open the right edge tracks the wall clock, so a
// running tool draws as a bar that grows. Below them, the event table —
// windowed and selection-synced with the lanes both ways — and the shared
// inspector (inspector.js).
//
// This module owns state and wiring. The pieces live in ./trajectory/:
// roles (what an event is), timeaxis (the axis math), model (spans, tracks),
// windowing (what the viewport needs), and the painters (lanes, table, hover
// card). None of them keeps a DOM node per event.

import { perFrame } from "./frame.js";
import { createInspector } from "./inspector.js";
import { store, subscribe } from "./store.js";
import { clock } from "./trajectory/format.js";
import { renderHoverCard } from "./trajectory/hovercard.js";
import { createLanes } from "./trajectory/lanes.js";
import { advanceLive, buildModel, emptyModel } from "./trajectory/model.js";
import { LANES, ROLES, configTarget as targetOf, innerOf, roleOf } from "./trajectory/roles.js";
import { createTable } from "./trajectory/table.js";
import {
  clampView, isFitted, planTicks, revealView, toReal, zoomView,
} from "./trajectory/timeaxis.js";
import { bottomTop, cullBars, hitTest, laneTrackAt } from "./trajectory/windowing.js";
import { debounce } from "./util.js";

// How often the live edge is re-pinned to the wall clock: often enough for a
// running tool's bar to look like it is growing, cheap because the tick only
// moves the open bars and never rebuilds the model.
const LIVE_TICK_MS = 250;
// The states store.js treats as the end of a turn. Anything else, or a tool
// called and not yet answered, means the right edge is still moving.
const SETTLED = new Set(["idle", "interrupted", "error"]);
// Our own scrolls must not read as "the user scrolled away".
const AUTO_SCROLL_GRACE_MS = 150;

let tableEl, detailEl, searchBox, followBtn;
let timelineEl, plotEl, hoverEl;
let lanes, table, inspector;

let activeFilters = new Set(ROLES);
let query = "";
let selectedSeq = null;
let collapseGaps = true;

// Live-follow: on by default, paused by any manual scroll-up, pan or row click,
// and only ever resumed by the toolbar button (which jumps to the newest event).
let following = true;
let autoScrollUntil = 0;
let pinNewest = false;       // the next paint scrolls the table to its end

let model = emptyModel();
let rows = [];
let hits = [];
let view = { v0: 0, span: 1000 };
let playV = null;            // playhead position, in virtual ms

// Geometry is cached from ResizeObservers: reading clientWidth per bar forced
// a layout per read, which was most of what a zoom frame used to cost.
let plotW = 0, laneH = 20, rowH = 24, tableH = 0;

let dirty = 0;               // 1 = model, 2 = lanes, 4 = rows
const paintSoon = perFrame(paintFrame);
let liveTimer = 0;

function isLive() {
  return store.runningTools.size > 0 || !SETTLED.has(store.agentStatus);
}

/** The configuration page that governs one event, or null. Knows the agent →
 *  definition map, so a subagent's events point at the definition it ran as. */
export function configTarget(ev, inner = innerOf(ev)) {
  return targetOf(ev, inner, model.agentDefs);
}

// ---- filters -------------------------------------------------------------

// Searching stringifies every event; a live session re-filters on each append,
// so each event's text is lowered once and kept.
const searchText = new WeakMap();

function visible(ev) {
  const role = roleOf(ev);
  if (!activeFilters.has(role) && role !== "META") return false;
  if (role === "META" && activeFilters.size !== ROLES.length) return false;
  if (query) {
    let s = searchText.get(ev);
    if (s === undefined) { s = JSON.stringify(ev).toLowerCase(); searchText.set(ev, s); }
    if (!s.includes(query)) return false;
  }
  return true;
}

// ---- boot ----------------------------------------------------------------

export function initTrajectory() {
  const $ = (id) => document.getElementById(id);
  tableEl = $("traj-table");
  timelineEl = $("traj-timeline");
  plotEl = $("tj-plot");
  hoverEl = $("tj-hover");
  detailEl = $("traj-detail");
  searchBox = $("traj-search");
  followBtn = $("traj-follow");

  lanes = createLanes({
    plotEl, gutterEl: $("tj-gutter"), bandsEl: $("tj-bands"), axisEl: $("tj-axis"),
    playEl: $("tj-playhead"), cursorEl: $("tj-cursor"),
  });
  table = createTable({ spacerEl: $("tj-spacer"), rowsEl: $("tj-rows") });
  inspector = createInspector(detailEl, {
    onClose: closeDetail,
    context: {
      target: (ev) => configTarget(ev),
      result: (ev) => {
        const inner = innerOf(ev);
        if (inner.type === "tool_result") return inner;
        return model.bySeq.get(ev.seq)?.result ?? null;
      },
      timing: (ev) => {
        const it = model.bySeq.get(ev.seq);
        return it && { t0: it.t0, t1: it.t1, running: it.running, inferred: it.inferred,
          tMin: model.tMin };
      },
    },
  });

  wireToolbar();
  wireTable();
  wirePlot();

  subscribe((kind, ev) => {
    if (kind === "reset") {
      // A new conversation starts at the live edge again.
      selectedSeq = null; playV = null; closeDetail();
      setFollowing(true); renderAll(); return;
    }
    if (kind === "replay_done") { renderAll(); fit(); return; }
    if (kind === "event" && !store.replaying) appendEvent();
    // A status flip is the only signal that a turn started or ended without a
    // logged event to go with it, and it decides whether "now" moves.
    if (kind === "status") invalidate(1);
  });

  measure();
  setFollowing(true);
  renderAll();
  fit();
}

function wireToolbar() {
  const filters = document.getElementById("traj-filters");
  for (const role of ROLES) {
    const b = document.createElement("button");
    b.type = "button";
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
  const syncGapBtn = () => {
    gapBtn.classList.toggle("is-on", collapseGaps);
    gapBtn.setAttribute("aria-pressed", collapseGaps ? "true" : "false");
    gapBtn.title = collapseGaps
      ? "Idle stretches are collapsed to a marked band — click for true scale"
      : "True scale: every idle minute takes its full width — click to collapse";
  };
  syncGapBtn();
  gapBtn.addEventListener("click", () => {
    collapseGaps = !collapseGaps;
    syncGapBtn();
    renderAll();
    fit();
  });
  document.getElementById("traj-export").addEventListener("click", exportLog);
}

function wireTable() {
  tableEl.addEventListener("scroll", () => {
    invalidate(4);
    // Scrolling away from the newest row is the gesture that means "let me read".
    if (!following || Date.now() < autoScrollUntil || !tableEl.clientHeight) return;
    if (tableEl.scrollHeight - tableEl.scrollTop - tableEl.clientHeight >= 24) setFollowing(false);
  });
  const rowsEl = document.getElementById("tj-rows");
  const activate = (e) => {
    // The row's config link is a real navigation, not a selection: doing both
    // would open configuration *and* rearrange the timeline behind it.
    if (e.target.closest("a")) return;
    const row = e.target.closest(".tj-row");
    if (row) selectSeq(Number(row.dataset.seq), { center: true });
  };
  rowsEl.addEventListener("click", activate);
  rowsEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(e); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const row = e.target.closest(".tj-row");
      if (row) { e.preventDefault(); stepRow(Number(row.dataset.seq), e.key === "ArrowDown" ? 1 : -1); }
    }
  });
  // Hovering a row points at its bar: the table and the lanes are one view.
  rowsEl.addEventListener("pointerover", (e) => {
    const row = e.target.closest(".tj-row");
    const it = row && model.bySeq.get(Number(row.dataset.seq));
    if (!it) return;
    const x = xOf(it.v0);
    if (x >= 0 && x <= plotW) lanes.showCursor(x, plotW, null); else lanes.hideCursor();
  });
  rowsEl.addEventListener("pointerleave", () => lanes.hideCursor());

  if (window.ResizeObserver) {
    // A hidden pane has no size, so rows that arrived while the panel was
    // closed left it parked at the top: re-anchor once it gets one.
    new ResizeObserver((entries) => {
      tableH = entries[0].contentRect.height;
      measure();
      if (following) pinNewest = true;
      invalidate(6);
    }).observe(tableEl);
    new ResizeObserver((entries) => {
      plotW = entries[0].contentRect.width;
      laneH = entries[0].contentRect.height / LANES.length || laneH;
      invalidate(2);
    }).observe(plotEl);
  }
}

function wirePlot() {
  plotEl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const x = e.clientX - plotEl.getBoundingClientRect().left;
    const horizontal = e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY);
    if (horizontal) {
      // A trackpad's horizontal flick is a pan, and panning is "let me read".
      panBy(((e.deltaX || e.deltaY) / Math.max(1, plotW)) * view.span);
      setFollowing(false);
    } else {
      // Zoom keeps follow alive: you are changing the lens, not looking away.
      zoomAt(x, Math.exp((e.deltaY * (e.ctrlKey ? 2.2 : 1)) * 0.0022));
    }
  }, { passive: false });

  let drag = null;
  plotEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, moved: 0, v0: view.v0 };
    plotEl.setPointerCapture(e.pointerId);
    plotEl.classList.add("is-dragging");
  });
  plotEl.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x;
      drag.moved = Math.max(drag.moved, Math.abs(dx), Math.abs(e.clientY - drag.y));
      if (drag.moved > 3) {
        setView(drag.v0 - (dx / Math.max(1, plotW)) * view.span, view.span);
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
    else if (e.key === "+" || e.key === "=") zoomAt(plotW / 2, 0.7);
    else if (e.key === "-" || e.key === "_") zoomAt(plotW / 2, 1.4);
    else if (e.key.toLowerCase() === "f") fit();
    else if (e.key === "." || e.key === ",") {
      // Step the selection through time without leaving the timeline.
      if (selectedSeq != null) stepRow(selectedSeq, e.key === "." ? 1 : -1, { fromPlot: true });
    } else return;
    e.preventDefault();
  });
}

// ---- model ---------------------------------------------------------------

function rebuild() {
  model = buildModel(store.events, {
    now: Date.now(), live: isLive(), isShown: visible, collapseGaps, prev: model,
  });
  rows = [];
  for (const it of model.items) if (it.shown) { it.row = rows.length; rows.push(it); }
}

function newestSeq() {
  return store.events.length ? store.events[store.events.length - 1].seq : null;
}

// ---- viewport ------------------------------------------------------------

function xOf(v) { return ((v - view.v0) / view.span) * plotW; }
function vOf(x) { return view.v0 + (x / Math.max(1, plotW)) * view.span; }

function setView(v0, span) {
  view = clampView(v0, span, model.totalV);
  invalidate(2);
}

function fit() { setView(0, model.totalV); }
function panBy(dv) { setView(view.v0 + dv, view.span); }

function zoomAt(px, k) {
  view = zoomView(view, px, Math.max(1, plotW), k, model.totalV);
  invalidate(2);
}

function ensureVisible(v, opts) {
  view = revealView(view, v, model.totalV, opts);
  invalidate(2);
}

// Reconcile the viewport with a model that just got longer. A fitted view keeps
// showing everything; a followed one keeps the live edge in frame at whatever
// zoom the reader chose; a view they panned somewhere else is left alone.
function keepUp(wasFit) {
  if (wasFit) fit();
  else if (following) ensureVisible(model.totalV);
}

function measure() {
  rowH = parseFloat(getComputedStyle(tableEl).getPropertyValue("--tj-row-h")) || 24;
  // Without a ResizeObserver these are the only readings there will be.
  if (!plotW) plotW = plotEl.clientWidth;
  if (!tableH) tableH = tableEl.clientHeight;
}

// ---- the moving present --------------------------------------------------

function syncLiveTicker() {
  const want = isLive();
  if (want === !!liveTimer) return;
  if (!want) {
    clearInterval(liveTimer);
    liveTimer = 0;
    // The turn is over: rebuild so the open bars settle onto the ends the log
    // recorded rather than whichever millisecond the ticker died on.
    invalidate(1);
    return;
  }
  liveTimer = setInterval(() => {
    if (!isLive()) { syncLiveTicker(); return; }
    // A hidden panel has no width: nothing to paint, no point walking the model.
    if (!plotW) return;
    const wasFit = isFitted(view, model.totalV);
    if (!advanceLive(model, Date.now())) return;
    keepUp(wasFit);
    invalidate(2);
  }, LIVE_TICK_MS);
}

// ---- painting ------------------------------------------------------------

function invalidate(bits) {
  dirty |= bits;
  paintSoon();
}

function paintFrame() {
  const d = dirty;
  dirty = 0;
  if (d & 1) {
    const wasFit = isFitted(view, model.totalV);
    rebuild();
    keepUp(wasFit);
    syncLiveTicker();
    if (following) followEdge();
    showDetail();
    // keepUp moves the viewport, which asked for the repaint we are doing now.
    dirty &= ~2;
  }
  if (d & 3) paintLanes();
  if (d & 5 || pinNewest) paintRows();
}

function renderAll() {
  measure();
  rebuild();
  syncLiveTicker();
  if (playV != null) playV = Math.min(playV, model.totalV);
  if (following) followEdge();
  showDetail();
  paintLanes();
  paintRows();
}

function paintLanes() {
  if (!plotW) return;
  const cull = cullBars(model.items, {
    view, W: plotW, laneCount: LANES.length, selectedSeq,
    newestSeq: following ? newestSeq() : null,
  });
  hits = cull.hits;
  lanes.paintBars(cull.bars, model.tracks, laneH);
  lanes.paintBands(model.segs, xOf, plotW);
  lanes.paintAxis(planTicks({
    segs: model.segs, view, W: plotW, live: model.live, tMin: model.tMin, tMax: model.tMax,
  }).ticks);
  lanes.paintPlayhead(playV == null ? null : xOf(playV), plotW);
  if (pointer) hoverAt();
}

function paintRows() {
  const viewH = tableH || tableEl.clientHeight || 400;
  let scrollTop;
  if (pinNewest) {
    pinNewest = false;
    // Grow the spacer first, or the browser clamps the scroll to the old end.
    table.size(rows.length, rowH);
    scrollTop = bottomTop(rows.length, rowH, viewH);
    autoScrollUntil = Date.now() + AUTO_SCROLL_GRACE_MS;
    tableEl.scrollTop = scrollTop;
  } else {
    scrollTop = tableEl.scrollTop;
  }
  table.paint({ rows, rowH, scrollTop, viewH, selectedSeq, targetOf: configTargetOf });
}

function configTargetOf(it) { return targetOf(it.ev, it.inner, model.agentDefs); }

// ---- live follow ---------------------------------------------------------

// Keep the newest event in view: the table at its end, the playhead on the
// newest bar, and the inspector — only while it is already open — on it too.
function followEdge() {
  pinNewest = true;
  const last = model.items[model.items.length - 1];
  if (last) {
    playV = last.v1;
    // Not ensureVisible: its padding would pan a fitted view off its own left
    // edge, and the next event would then find it "zoomed in".
    keepUp(isFitted(view, model.totalV));
  }
  const seq = newestSeq();
  if (seq != null && !detailEl.classList.contains("hidden")) {
    selectedSeq = seq;
    showDetail();
  }
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
  if (on && jump) followEdge();
  invalidate(6);
}

function appendEvent() {
  invalidate(1);
}

// ---- interaction ---------------------------------------------------------

// Where the pointer is over the plot, in plot coordinates, plus the plot's
// offset in the timeline for placing the card. Kept so a repaint under a still
// pointer (zooming with the wheel, a live bar growing) re-answers "what is
// under it" without another layout read.
let pointer = null;
let cardW = 0;               // the card's width is set by CSS, not by its content
let cardKey = "";            // what the card shows, so a still pointer does not rebuild it
let cardEv = null;

function onHover(e) {
  const rect = plotEl.getBoundingClientRect();
  const host = timelineEl.getBoundingClientRect();
  pointer = { x: e.clientX - rect.left, y: e.clientY - rect.top,
    off: rect.left - host.left, hostW: host.width };
  if (!hoverEl.classList.contains("hidden")) cardW = hoverEl.offsetWidth || cardW;
  hoverAt();
}

function hoverAt() {
  const { x, y, off, hostW } = pointer;
  if (x < 0 || x > plotW || y < 0) { hideHover(); return; }
  lanes.showCursor(x, plotW, clock(toReal(model.segs, vOf(x)), { ms: view.span < 20000 }));
  const h = hitTest(hits, x, laneTrackAt(y, laneH, model.tracks));
  if (!h) { hoverEl.classList.add("hidden"); cardKey = ""; return; }
  // Keyed on the span too: a call that settles under a still pointer must stop
  // saying "still running".
  const key = `${h.it.seq}|${h.it.t0}|${h.it.t1}|${h.it.running}|${model.tMin}`;
  if (key !== cardKey || h.it.ev !== cardEv) {
    cardKey = key;
    cardEv = h.it.ev;
    renderHoverCard(hoverEl, h.it, { tMin: model.tMin, target: configTarget(h.it.ev, h.it.inner) });
  }
  hoverEl.classList.remove("hidden");
  if (!cardW) cardW = hoverEl.offsetWidth || 240;
  const w = cardW;
  let left = off + x + 14;
  if (left + w > hostW - 6) left = Math.max(4, off + x - w - 14);
  hoverEl.style.left = left.toFixed(1) + "px";
}

function hideHover() {
  pointer = null;
  lanes.hideCursor();
  hoverEl.classList.add("hidden");
  cardKey = "";
}

function onPlotClick(e) {
  const rect = plotEl.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const h = hitTest(hits, x, laneTrackAt(e.clientY - rect.top, laneH, model.tracks));
  playV = vOf(x);
  if (h) selectSeq(h.it.seq, { scroll: true, keepPlayhead: true });
  else invalidate(2);
}

// Move the selection to the previous/next row of the table.
function stepRow(seq, dir, { fromPlot = false } = {}) {
  const it = model.bySeq.get(seq);
  const next = it && it.row >= 0 ? rows[it.row + dir] : null;
  if (!next) return;
  selectSeq(next.seq, { scroll: true });
  if (!fromPlot) {
    // Keep the keyboard on the table: the row node exists once painted.
    requestAnimationFrame(() => table.rowFor(next.seq)?.focus({ preventScroll: true }));
  }
}

/** Select an event: the row, its bar, the playhead and the inspector all move
 *  to it. Used by the table, the lanes and — through inspect.js — every other
 *  surface that can name an event by its sequence number. */
export function selectSeq(seq, { scroll = false, center = false, keepFollow = false,
  keepPlayhead = false } = {}) {
  // Picking an event by hand is the other way to say "let me read".
  if (!keepFollow && following) setFollowing(false);
  selectedSeq = seq;
  const it = model.bySeq.get(seq);
  if (it) {
    if (!keepPlayhead) { playV = it.v0; ensureVisible(it.v0, { center }); }
    if (it.row >= 0 && (scroll || center)) {
      const viewH = tableH || tableEl.clientHeight;
      const top = it.row * rowH;
      // Only move when the row is out of view: a click on a visible row must
      // not yank the table out from under the pointer.
      if (top < tableEl.scrollTop || top + rowH > tableEl.scrollTop + viewH) {
        autoScrollUntil = Date.now() + AUTO_SCROLL_GRACE_MS;
        tableEl.scrollTop = Math.max(0, top - viewH / 2);
      }
    }
  }
  detailEl.classList.remove("hidden");
  showDetail();
  invalidate(6);
}

function closeDetail() {
  selectedSeq = null;
  detailKey = "";
  detailEl.classList.add("hidden");
  inspector.show(null);
  invalidate(6);
}

// The inspector shows a snapshot, so it is redrawn when the selection moves to
// another event and when the selected one changes under it: a result landing,
// or a running call settling, changes what Result and Timing should say.
let detailKey = "";
function showDetail() {
  if (detailEl.classList.contains("hidden") || selectedSeq == null) return;
  const it = model.bySeq.get(selectedSeq);
  const ev = it?.ev ?? store.events.find((e) => e.seq === selectedSeq) ?? null;
  const key = it ? `${it.seq}|${it.result ? 1 : 0}|${it.running ? 1 : 0}` : String(selectedSeq);
  if (inspector.event === ev && key === detailKey) return;
  detailKey = key;
  inspector.show(ev);
}

function exportLog() {
  const blob = new Blob([JSON.stringify(store.events, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `quickcode-${store.convId || "session"}-trajectory.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}
