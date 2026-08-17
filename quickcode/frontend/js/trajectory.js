// Trajectory view: the append-only event log, inspectable by source.
// Timeline strip + dense table + right-hand detail inspector
// (Summary / Payload / Result / Timing), DeepSeek-Harness-style.

import { store, subscribe, toolResultFor } from "./store.js";
import { clickable, debounce, el, esc, fmtMs, fmtTime, fmtTokens, oneLine } from "./util.js";

const ROLES = ["SYSTEM", "USER", "CONTEXT", "ASSISTANT", "TOOL", "REVIEW", "AGENT", "ERROR"];

let table, timeline, detail, detailBody, detailTitle, searchBox, followBtn;
let activeFilters = new Set(ROLES);
let query = "";
let selectedSeq = null;
let detailTab = "summary";
// Live-follow: on by default, paused by any manual scroll-up or row click, and
// only ever resumed by the toolbar button (which jumps to the newest event).
let following = true;
let autoScrollUntil = 0;     // our own scrolls must not read as "user scrolled"

export function roleOf(ev) {
  const inner = ev.type === "agent_event" ? ev.ev || {} : ev;
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
    case "agent_spawned": return `spawned ${ev.agent_id} (${ev.definition})`;
    case "system_note": return inner.text;
    case "usage": return `tokens in ${fmtTokens(inner.input_tokens)} / out ${fmtTokens(inner.output_tokens)}`;
    case "error": return inner.message;
    default: return oneLine(JSON.stringify(inner), 160);
  }
}

export function initTrajectory() {
  table = document.getElementById("traj-table");
  timeline = document.getElementById("traj-timeline");
  detail = document.getElementById("traj-detail");
  detailBody = document.getElementById("detail-body");
  detailTitle = document.getElementById("traj-detail-title");
  searchBox = document.getElementById("traj-search");
  followBtn = document.getElementById("traj-follow");

  followBtn.addEventListener("click", () => setFollowing(!following, { jump: true }));
  // Scrolling away from the newest row is the gesture that means "let me read".
  table.addEventListener("scroll", () => {
    if (!following || Date.now() < autoScrollUntil || !table.clientHeight) return;
    if (!atBottom()) setFollowing(false);
  });
  // A hidden pane has no scroll height, so rows that arrived while the panel
  // was closed leave it parked at the top: re-anchor once it gets a size.
  if (window.ResizeObserver) {
    new ResizeObserver(() => { if (following) scrollToNewest(); }).observe(table);
  }

  const filters = document.getElementById("traj-filters");
  for (const role of ROLES) {
    const b = el(`<button class="tfilter active" style="color:var(--chip-${role.toLowerCase()})">${role}</button>`);
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

  document.getElementById("traj-export").addEventListener("click", exportLog);
  document.getElementById("traj-detail-close").addEventListener("click", () => {
    selectedSeq = null;
    detail.classList.add("hidden");
    table.querySelectorAll(".trow.selected").forEach((r) => r.classList.remove("selected"));
    timeline.querySelectorAll(".tl-block.selected").forEach((r) => r.classList.remove("selected"));
  });
  document.getElementById("detail-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    detailTab = b.dataset.tab;
    document.querySelectorAll("#detail-tabs button").forEach((x) =>
      x.classList.toggle("active", x === b));
    renderDetail();
  });

  subscribe((kind, ev) => {
    // A new conversation starts at the live edge again.
    if (kind === "reset") { selectedSeq = null; setFollowing(true); renderAll(); return; }
    if (kind === "replay_done") { renderAll(); return; }
    if (kind === "event" && !store.replaying) appendRow(ev);
  });
  setFollowing(true);
  renderAll();
}

// ---- live follow ----

function atBottom() {
  return table.scrollHeight - table.scrollTop - table.clientHeight < 24;
}

function newestSeq() {
  return store.events.length ? store.events[store.events.length - 1].seq : null;
}

function scrollToNewest() {
  // The scroll event lands a tick later; a short window is what tells our own
  // scroll apart from the user's.
  autoScrollUntil = Date.now() + 150;
  table.scrollTop = table.scrollHeight;
  timeline.scrollTop = timeline.scrollHeight;
}

// While following, the newest block wears a marker so the timeline shows where
// the live edge is even when the table is scrolled elsewhere.
function markNewest() {
  timeline.querySelectorAll(".tl-block.newest").forEach((b) => b.classList.remove("newest"));
  if (following) timeline.lastElementChild?.classList.add("newest");
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
  markNewest();
}

function visible(ev) {
  if (!activeFilters.has(roleOf(ev)) && roleOf(ev) !== "META") return false;
  if (roleOf(ev) === "META" && activeFilters.size !== ROLES.length) return false;
  if (query && !JSON.stringify(ev).toLowerCase().includes(query)) return false;
  return true;
}

function rowNode(ev) {
  const role = roleOf(ev);
  const inner = ev.type === "agent_event" ? ev.ev || {} : ev;
  const isErr = inner.is_error || inner.type === "error";
  const ms = inner.type === "tool_result" && inner.ms ? fmtMs(inner.ms) : "";
  const row = el(`<div class="trow ${isErr ? "is-error" : ""}" data-seq="${ev.seq}">
    <span class="seq">${ev.seq}</span>
    <span class="chip chip-${role}">${role}</span>
    <span class="preview">${esc(previewOf(ev))}</span>
    <span class="t-ms">${ms}</span></div>`);
  clickable(row, () => selectSeq(ev.seq));
  return row;
}

function blockNode(ev) {
  const role = roleOf(ev);
  // The strip is a mouse-only mini-map of the table above it: every block is
  // reachable as a row, so it stays out of the tab order and off the a11y tree
  // rather than adding hundreds of duplicate stops.
  const b = el(`<span class="tl-block" data-seq="${ev.seq}" aria-hidden="true"
     style="background:var(--chip-${role.toLowerCase()})"
     title="#${ev.seq} ${role}: ${esc(oneLine(previewOf(ev), 80))}"></span>`);
  b.addEventListener("click", () => selectSeq(ev.seq, { scroll: true }));
  return b;
}

function renderAll() {
  table.innerHTML = "";
  timeline.innerHTML = "";
  for (const ev of store.events) {
    timeline.appendChild(blockNode(ev));
    if (visible(ev)) table.appendChild(rowNode(ev));
  }
  if (selectedSeq != null) highlight(selectedSeq);
  if (following) scrollToNewest();
  markNewest();
}

function appendRow(ev) {
  timeline.appendChild(blockNode(ev));
  if (visible(ev)) table.appendChild(rowNode(ev));
  if (!following) return;
  scrollToNewest();
  // The inspector tracks the live edge only while it is already open.
  if (!detail.classList.contains("hidden")) selectSeq(ev.seq, { keepFollow: true });
  markNewest();
}

export function selectSeq(seq, { scroll = false, keepFollow = false } = {}) {
  // Picking an event by hand is the other way to say "let me read".
  if (!keepFollow && following) setFollowing(false);
  selectedSeq = seq;
  highlight(seq);
  if (scroll) {
    const row = table.querySelector(`.trow[data-seq="${seq}"]`);
    if (row) row.scrollIntoView({ block: "center" });
  }
  detail.classList.remove("hidden");
  renderDetail();
}

function highlight(seq) {
  table.querySelectorAll(".trow.selected").forEach((r) => r.classList.remove("selected"));
  timeline.querySelectorAll(".tl-block.selected").forEach((r) => r.classList.remove("selected"));
  table.querySelector(`.trow[data-seq="${seq}"]`)?.classList.add("selected");
  timeline.querySelector(`.tl-block[data-seq="${seq}"]`)?.classList.add("selected");
}

function renderDetail() {
  const ev = store.events.find((e) => e.seq === selectedSeq);
  if (!ev) { detailBody.innerHTML = ""; return; }
  const role = roleOf(ev);
  const inner = ev.type === "agent_event" ? ev.ev || {} : ev;
  detailTitle.innerHTML = `<span class="chip chip-${role}" style="padding:1px 8px;border-radius:4px;color:var(--bg)">${role}</span>
    &nbsp;#${ev.seq} · ${esc(inner.type || ev.type)}`;

  if (detailTab === "summary") {
    const rows = [
      ["Type", inner.type || ev.type],
      ["Turn", ev.turn ?? "–"],
      ["Sequence", ev.seq],
      ev.agent_id ? ["Agent", ev.agent_id] : null,
      inner.name ? ["Tool", inner.name] : null,
      inner.finish_reason ? ["Finish", inner.finish_reason] : null,
      inner.is_error != null ? ["Status", inner.is_error ? "error" : "ok"] : null,
    ].filter(Boolean);
    const kv = rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${esc(String(v))}</div>`).join("");
    const text = inner.text || inner.content || inner.arguments || inner.plan || inner.message || "";
    detailBody.innerHTML = `<div class="kv">${kv}</div>` +
      (text ? `<pre>${esc(String(text).slice(0, 20000))}</pre>` : "");
  } else if (detailTab === "payload") {
    detailBody.innerHTML = `<pre>${esc(JSON.stringify(ev, null, 2).slice(0, 60000))}</pre>`;
  } else if (detailTab === "result") {
    let result = null;
    if (inner.type === "tool_call") result = toolResultFor(inner.id);
    else if (inner.type === "tool_result") result = inner;
    detailBody.innerHTML = result
      ? `<pre>${esc(String(result.content).slice(0, 60000))}</pre>`
      : `<div class="kv"><div class="k">Result</div><div class="v">— not applicable —</div></div>`;
  } else if (detailTab === "timing") {
    const rows = [
      ["Started", fmtTime(ev.ts) || "–"],
      inner.ms != null ? ["Duration", fmtMs(inner.ms)] : null,
      ["Source", ev.agent_id ? `subagent ${ev.agent_id}` : "main agent"],
    ].filter(Boolean);
    detailBody.innerHTML = `<div class="kv">${rows.map(([k, v]) =>
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
