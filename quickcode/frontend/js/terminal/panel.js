// The terminal dock: a horizontal drawer under the transcript.
//
// Why the bottom and not a sixth tab in the side panel. The side panel is a
// column of *readouts* — things you consult about the session. A shell is
// something you work in, it is wide rather than tall, and it is the one
// surface here that competes with the composer for your hands. Every editor
// that has both puts the panel on the right and the terminal along the bottom
// for those reasons, and the user asked for it there.
//
// Two tabs, and the split is by who is typing:
//
//   Shell   — the live pty. The user's own shell, in the project directory.
//   Agent   — every `bash` the agent ran, read-only (see agentfeed.js).
//
// The height, the open state and the chosen tab are remembered per project,
// like the side panel's width — "I keep a terminal open in this repo" is a
// per-repo habit.

import { initAgentFeed } from "./agentfeed.js";
import { keyToBytes } from "./keys.js";
import { TerminalSocket } from "./socket.js";
import { TerminalView } from "./view.js";

const $ = (id) => document.getElementById(id);

const MIN_H = 120;
const DEFAULT_H = 280;
const maxHeight = () => Math.round(window.innerHeight * 0.8);

let dock, grip, screenHost, statusEl, tabsEl;
let view = null;
let socket = null;
let projectId = null;
let started = false;              // has a shell ever been asked for?
let state = { open: false, tab: "shell", height: DEFAULT_H };

// ---- persistence ----

const storeKey = (pid) => `qc-terminal:${pid || "default"}`;

function load(pid) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(storeKey(pid)) || "{}"); } catch { /* corrupt */ }
  return {
    open: saved.open === true,
    tab: saved.tab === "agent" ? "agent" : "shell",
    height: clampHeight(Number(saved.height) || DEFAULT_H),
  };
}

function save() {
  try { localStorage.setItem(storeKey(projectId), JSON.stringify(state)); } catch { /* quota */ }
}

function clampHeight(h) {
  // Rounded: a pointer drag produces fractional pixels, and a stored height of
  // 620.7799987792969 is a number nobody ever needs to see again.
  return Math.round(Math.max(MIN_H, Math.min(h, Math.max(MIN_H, maxHeight()))));
}

// ---- rendering ----

function apply() {
  dock.classList.toggle("open", state.open);
  dock.style.setProperty("--term-h", state.height + "px");
  dock.setAttribute("aria-hidden", state.open ? "false" : "true");
  for (const btn of tabsEl.querySelectorAll("[data-tab]")) {
    const on = btn.dataset.tab === state.tab;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  }
  for (const pane of dock.querySelectorAll(".qt-pane")) {
    pane.classList.toggle("active", pane.dataset.pane === state.tab);
  }
  const toggle = $("btn-term-toggle");
  if (toggle) toggle.classList.toggle("on", state.open);
  if (state.open && state.tab === "shell") ensureShell();
  fit();
}

function setStatus(kind, detail) {
  const words = {
    idle: "not started",
    starting: "starting a shell…",
    live: detail || "live",
    exited: `shell ended${detail ? " — " + detail : ""}`,
    closed: "disconnected",
    denied: "refused — reload the window",
    gone: "that project is not open any more",
    failed: `could not start a shell${detail ? " — " + detail : ""}`,
  };
  statusEl.textContent = words[kind] || kind;
  statusEl.dataset.kind = kind;
  dock.classList.toggle("qt-dead", kind === "exited" || kind === "closed" || kind === "failed");
}

// ---- the shell ----

function ensureShell() {
  if (started) return;
  startShell();
}

function startShell() {
  started = true;
  view.clear();
  socket.open(projectId);
}

/** Send the panel's real size to the pty, so full-screen programs line up. */
function fit() {
  if (!state.open || !view) return;
  const { rows, cols } = view.measure();
  view.resize(rows, cols);
  if (socket) socket.resize(rows, cols);
}

// ---- public API ----

export function setTerminalProject(pid) {
  if (pid === projectId) return;
  // Leaving a project ends its shell: the socket closing is what kills the
  // process tree server-side, and a shell from the last project sitting in the
  // next project's panel would be the worst of both.
  if (socket) socket.close();
  started = false;
  if (view) view.clear();
  projectId = pid;
  state = load(pid);
  setStatus("idle", "");
  apply();
}

export function toggleTerminal(force) {
  state.open = force === undefined ? !state.open : !!force;
  save();
  apply();
  if (state.open && state.tab === "shell") focusShell();
}

export function openTerminalTab(tab) {
  state.open = true;
  state.tab = tab;
  save();
  apply();
  if (tab === "shell") focusShell();
}

function focusShell() {
  requestAnimationFrame(() => screenHost.focus({ preventScroll: true }));
}

/** Put a command at the prompt. Never with a newline — see agentfeed.js. */
function stageCommand(command) {
  const text = String(command || "").replace(/[\r\n]+/g, " ").trim();
  if (!text) return;
  openTerminalTab("shell");
  if (!socket.live) return;
  socket.input(text);
  focusShell();
}

// ---- wiring ----

export function initTerminal() {
  dock = $("term-dock");
  grip = $("term-grip");
  tabsEl = $("term-tabs");
  screenHost = $("term-screen");
  statusEl = $("term-status");

  view = new TerminalView(screenHost);
  socket = new TerminalSocket({
    onOutput: (data) => view.write(data),
    onReady: () => fit(),
    onStatus: (kind, detail) => setStatus(kind, detail),
  });
  initAgentFeed($("term-agent"), { onRerun: stageCommand });

  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) openTerminalTab(b.dataset.tab);
  });
  $("btn-term-close").addEventListener("click", () => toggleTerminal(false));
  $("btn-term-clear").addEventListener("click", () => {
    view.clear();
    // A cleared screen the shell does not know about would leave its prompt
    // half-way down; ^L is how a terminal asks for a redraw.
    if (socket.live) socket.input("\x0c");
    focusShell();
  });
  $("btn-term-restart").addEventListener("click", () => { startShell(); focusShell(); });
  const toggle = $("btn-term-toggle");
  if (toggle) toggle.addEventListener("click", () => toggleTerminal());

  initKeyboard();
  initGrip();

  window.addEventListener("resize", () => {
    const h = clampHeight(state.height);
    if (h !== state.height) { state.height = h; }
    apply();
  });

  apply();
}

function initKeyboard() {
  screenHost.addEventListener("keydown", (e) => {
    // The panel's own shortcut wins over anything it would otherwise send.
    if (e.key === "`" && (e.ctrlKey || e.metaKey)) return;
    const bytes = keyToBytes(e);
    if (bytes === null) return;               // browser keeps it (copy, paste)
    e.preventDefault();
    e.stopPropagation();
    if (!socket.live) return;
    socket.input(bytes);
  });
  screenHost.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (text && socket.live) socket.input(text.replace(/\r\n/g, "\r").replace(/\n/g, "\r"));
  });
  // Ctrl+` from anywhere: the one shortcut a terminal panel is expected to have.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "`" || !(e.ctrlKey || e.metaKey) || e.altKey) return;
    if (document.querySelector(".modal-backdrop")) return;
    e.preventDefault();
    toggleTerminal();
  });
}

function initGrip() {
  let dragging = false;
  let startY = 0;
  let startH = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = state.height;
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    document.body.classList.add("resizing-terminal");
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    // Dragging the grip *up* makes the dock taller, so the delta is inverted.
    state.height = clampHeight(startH + (startY - e.clientY));
    dock.style.setProperty("--term-h", state.height + "px");
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    try { grip.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
    grip.classList.remove("dragging");
    document.body.classList.remove("resizing-terminal");
    save();
    fit();
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
  grip.addEventListener("dblclick", () => {
    state.height = clampHeight(DEFAULT_H);
    save();
    apply();
  });
}
