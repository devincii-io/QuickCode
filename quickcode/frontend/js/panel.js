// The right-hand side panel: one tab strip over five panes — Trajectory plus
// the four panel-contract modules (Agents, Tasks, Files, Usage).
//
// Trajectory is not special-cased beyond its markup: its DOM lives in
// index.html (trajectory.js binds to those ids at boot) and this module only
// shows and hides the pane around it. The other four are mounted once into
// containers they own outright, per the panel contract.
//
// Open state, active tab, width and maximization are remembered per project,
// because "I keep the trajectory open in this repo" is a per-repo habit.

import { panel as agentsPanel } from "./panels/agents.js";
import { panel as filesPanel } from "./panels/files.js";
import { panel as tasksPanel } from "./panels/tasks.js";
import { panel as usagePanel } from "./panels/usage.js";

const $ = (id) => document.getElementById(id);

const MIN_W = 320;
const DEFAULT_W = 520;
const maxWidth = () => Math.round(window.innerWidth * 0.7);

// Trajectory first: it is the panel's reason to exist.
const TABS = [
  { id: "trajectory", title: "Trajectory", icon: "⌁", module: null },
  { id: agentsPanel.id, title: agentsPanel.title, icon: agentsPanel.icon, module: agentsPanel },
  { id: tasksPanel.id, title: tasksPanel.title, icon: tasksPanel.icon, module: tasksPanel },
  { id: filesPanel.id, title: filesPanel.title, icon: filesPanel.icon, module: filesPanel },
  { id: usagePanel.id, title: usagePanel.title, icon: usagePanel.icon, module: usagePanel },
];

let main, aside, grip, tabsEl;
let projectId = null;
let state = { open: false, tab: "trajectory", width: DEFAULT_W, max: false };
const mounted = new Set();

// ---- persistence ----

function storeKey(pid) { return `qc-panel:${pid || "default"}`; }

function load(pid) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(storeKey(pid)) || "{}"); } catch { /* corrupt */ }
  const tab = TABS.some((t) => t.id === saved.tab) ? saved.tab : "trajectory";
  return {
    open: saved.open === true,
    tab,
    width: clampWidth(Number(saved.width) || DEFAULT_W),
    max: saved.max === true,
  };
}

function save() {
  try { localStorage.setItem(storeKey(projectId), JSON.stringify(state)); } catch { /* quota */ }
}

function clampWidth(w) {
  return Math.max(MIN_W, Math.min(w, Math.max(MIN_W, maxWidth())));
}

// ---- rendering ----

function apply() {
  main.classList.toggle("panel-open", state.open);
  main.classList.toggle("panel-max", state.open && state.max);
  aside.style.setProperty("--panel-w", state.width + "px");
  aside.setAttribute("aria-hidden", state.open ? "false" : "true");
  for (const t of TABS) {
    const btn = tabsEl.querySelector(`[data-tab="${t.id}"]`);
    if (btn) {
      const on = t.id === state.tab;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }
    const pane = main.querySelector(`.panel-pane[data-pane="${t.id}"]`);
    if (pane) pane.classList.toggle("active", t.id === state.tab);
  }
  const maxBtn = $("btn-panel-max");
  maxBtn.textContent = state.max ? "⤡" : "⛶";
  maxBtn.title = state.max ? "Restore the chat (Esc)" : "Maximize the panel";
  const toggle = $("btn-panel-toggle");
  if (toggle) toggle.classList.toggle("on", state.open);
  mountActive();
}

// Panels are mounted lazily but never unmounted: each seeds itself from the
// store and keeps itself current, so a mounted-then-hidden pane stays right.
function mountActive() {
  if (!state.open) return;
  const tab = TABS.find((t) => t.id === state.tab);
  if (!tab || !tab.module || mounted.has(tab.id)) return;
  const pane = main.querySelector(`.panel-pane[data-pane="${tab.id}"]`);
  if (!pane) return;
  mounted.add(tab.id);
  tab.module.init(pane);
}

// ---- public API ----

export function setPanelProject(pid) {
  projectId = pid;
  state = load(pid);
  apply();
}

export function openPanelTab(id) {
  if (!TABS.some((t) => t.id === id)) return;
  state.open = true;
  state.tab = id;
  save();
  apply();
}

export function togglePanel(force) {
  state.open = force === undefined ? !state.open : !!force;
  if (!state.open) state.max = false;
  save();
  apply();
}

export function toggleMaximize(force) {
  if (!state.open) state.open = true;
  state.max = force === undefined ? !state.max : !!force;
  save();
  apply();
}

export function panelIsMaximized() { return state.open && state.max; }

// ---- wiring ----

export function initPanel() {
  main = $("main");
  aside = $("side-panel");
  grip = $("panel-grip");
  tabsEl = $("panel-tabs");

  tabsEl.innerHTML = TABS.map((t) => `
    <button class="panel-tab" data-tab="${t.id}" role="tab" title="${t.title}">
      <span class="pt-icon">${t.icon}</span><span class="pt-label">${t.title}</span>
    </button>`).join("");

  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (!b) return;
    // Clicking the active tab of an open panel closes it — the tab strip
    // doubles as the panel's own toggle.
    if (state.open && state.tab === b.dataset.tab) togglePanel(false);
    else openPanelTab(b.dataset.tab);
  });

  $("btn-panel-close").addEventListener("click", () => togglePanel(false));
  $("btn-panel-max").addEventListener("click", () => toggleMaximize());
  $("btn-panel-toggle").addEventListener("click", () => togglePanel());

  initGrip();

  // Esc leaves maximization. The composer also listens for Esc (interrupt), so
  // this only claims the key when the panel actually owns the screen and the
  // keystroke did not come from the composer.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !panelIsMaximized()) return;
    if (document.querySelector(".modal-backdrop, .menu")) return;
    if (e.target.closest && e.target.closest("#composer")) return;
    e.stopImmediatePropagation();
    e.preventDefault();
    toggleMaximize(false);
  }, true);

  window.addEventListener("resize", () => {
    const w = clampWidth(state.width);
    if (w !== state.width) { state.width = w; apply(); }
  });

  apply();
}

function initGrip() {
  let dragging = false;
  grip.addEventListener("pointerdown", (e) => {
    if (state.max) return;                 // nothing to resize when full-width
    dragging = true;
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    document.body.classList.add("resizing-panel");
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    state.width = clampWidth(window.innerWidth - e.clientX);
    aside.style.setProperty("--panel-w", state.width + "px");
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    try { grip.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
    grip.classList.remove("dragging");
    document.body.classList.remove("resizing-panel");
    save();
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
  // Double-click resets to a sane default rather than leaving a 320px sliver.
  grip.addEventListener("dblclick", () => {
    state.width = clampWidth(DEFAULT_W);
    save();
    apply();
  });
}
