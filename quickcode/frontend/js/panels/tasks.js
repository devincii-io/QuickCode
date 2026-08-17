// Tasks panel: read-only view of the agent's task board (the old TUI tasks
// pane). Grouped by status with a completion bar, dependency and owner chips.
// The model mutates the board through its task tools; this panel never writes.
//
// INTEGRATION
//   import { panel as tasksPanel } from "./panels/tasks.js";
//   tasksPanel.init(containerDiv);            // call exactly once
// Assumptions the host must honour:
//   - `init(container)` is called at most once, with a container this module
//     owns outright (it sets innerHTML and adds the `.panel-tasks` class).
//   - The container has a definite height (flex column; the list scrolls).
//   - css/panels.css is linked; every selector here is namespaced under
//     `.panel-tasks`.
//   - Board snapshots arrive on the "tasks" and "state" notifications; the
//     panel seeds itself from `store.state.tasks` at init, so mounting late
//     (after the first state event) still shows the current board.

import { store, subscribe } from "../store.js";
import { el, esc, oneLine } from "../util.js";

const MARKS = { in_progress: "◐", pending: "○", completed: "✓" };
const SECTIONS = [
  ["in_progress", "In progress"],
  ["pending", "Pending"],
  ["completed", "Completed"],
];

let root = null;
let tasks = [];   // last seen board snapshot

export const panel = {
  id: "tasks",
  title: "Tasks",
  icon: "☑",
  init(container) {
    root = container;
    root.classList.add("panel-tasks");
    root.innerHTML = `
      <div class="pt-progress">
        <div class="pt-bar"><i style="width:0%"></i></div>
        <span class="pt-count"></span>
      </div>
      <div class="pt-list"></div>`;
    if (Array.isArray(store.state?.tasks)) tasks = store.state.tasks;
    subscribe(onStoreChange);
    render();
  },
};

function onStoreChange(kind, ev) {
  if (kind === "reset") { tasks = []; return render(); }
  if (kind === "tasks" || (kind === "state" && ev?.tasks)) {
    tasks = Array.isArray(ev.tasks) ? ev.tasks : [];
    render();
  }
}

// ---- rendering ----

function render() {
  if (!root) return;
  const live = tasks.filter((t) => t && t.status !== "deleted");
  const byId = new Map(live.map((t) => [t.id, t]));
  const done = live.filter((t) => t.status === "completed").length;
  const pct = live.length ? Math.round((done / live.length) * 100) : 0;

  root.querySelector(".pt-bar i").style.width = pct + "%";
  root.querySelector(".pt-count").textContent =
    live.length ? `${done}/${live.length}` : "";
  root.classList.toggle("pt-complete", live.length > 0 && done === live.length);

  const list = root.querySelector(".pt-list");
  const keep = list.scrollTop;
  list.innerHTML = "";

  if (!live.length) {
    list.appendChild(el(`<div class="pt-empty">
      <div class="pt-empty-mark">☑</div>
      <div class="pt-empty-title">No tasks on the board</div>
      <div class="pt-empty-hint">The agent keeps its own checklist here while it works
        through anything multi-step. Ask for a plan, or for work with several parts,
        and the board fills itself in.</div>
    </div>`));
    return;
  }

  for (const [status, label] of SECTIONS) {
    const group = live.filter((t) => t.status === status);
    if (!group.length) continue;
    const sec = el(`<div class="pt-section pt-${esc(status)}">
      <div class="pt-sec-head">
        <span class="pt-sec-mark">${MARKS[status]}</span>
        <span class="pt-sec-title">${esc(label)}</span>
        <span class="pt-sec-n">${group.length}</span>
      </div>
      <div class="pt-rows"></div></div>`);
    const rows = sec.querySelector(".pt-rows");
    for (const t of group) rows.appendChild(rowNode(t, byId));
    list.appendChild(sec);
  }
  list.scrollTop = keep;
}

function rowNode(t, byId) {
  // While a task runs, the board's active_form ("Refactoring the parser") reads
  // better than the imperative subject; the subject stays in the tooltip.
  const label = t.status === "in_progress" && t.active_form ? t.active_form : t.subject;
  const tip = [t.subject, t.description].filter(Boolean).map((s) => oneLine(s, 400)).join(" — ");

  const blockers = (t.blocked_by || [])
    .filter((id) => byId.get(id)?.status !== "completed")
    .map((id) => `<span class="pt-chip pt-blocked" title="blocked by ${esc(id)}">⛔ ${esc(id)}</span>`)
    .join("");
  const owner = t.owner
    ? `<span class="pt-chip pt-owner" title="owner">${esc(t.owner)}</span>` : "";

  return el(`<div class="pt-task pt-st-${esc(t.status)}"${tip ? ` title="${esc(tip)}"` : ""}>
    <span class="pt-mark">${MARKS[t.status] || "○"}</span>
    <span class="pt-id">${esc(t.id)}</span>
    <span class="pt-subject">${esc(label || "")}</span>
    <span class="pt-chips">${blockers}${owner}</span>
  </div>`);
}
