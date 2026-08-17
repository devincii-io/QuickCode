// Composer: the message input and everything that hangs off it — autosize,
// send / interrupt / compact, mode + model pills, input history (localStorage)
// and the slash-command menu. Parity with the old Textual TUI composer.

import { currentProject } from "./api.js";
import { openHelp, openModeMenu, openModelMenu } from "./modals.js";
import { esc } from "./util.js";
import { actions } from "./ws.js";

const $ = (id) => document.getElementById(id);

const HISTORY_KEY = "qc-history";
const HISTORY_MAX = 100;

const MODE_DESCS = [
  ["plan", "Read-only exploration; the agent submits a plan first."],
  ["ask", "Every mutating action asks for permission."],
  ["auto-edit", "Edits inside the project run; shell still asks."],
  ["dontask", "Never prompts — anything outside the rules is denied."],
  ["yolo", "No permission prompts at all (needs --yolo)."],
];

// ---- history (project-scoped, shared across conversations) ----

// One list per project, like the side panel's layout: the messages you sent to
// one repo are noise in the next.
function historyKey() { return `${HISTORY_KEY}:${currentProject() || "default"}`; }

function loadHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(historyKey()) || "[]");
    return Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : [];
  } catch { return []; }
}

function saveHistory(h) {
  try { localStorage.setItem(historyKey(), JSON.stringify(h)); } catch { /* quota / private mode */ }
}

// The composer is wired once, before any project is open, so the list is
// (re)read the first time each project needs it.
function syncHistory() {
  const pid = currentProject() || "default";
  if (pid === historyPid) return;
  historyPid = pid;
  history = loadHistory();
  histIdx = null;
  draft = "";
}

// ---- module state ----

let input = null;
let hooks = { onNewConversation: () => {} };
let history = [];
let historyPid = null;
let histIdx = null;   // null = editing the draft, else index into history
let draft = "";
let menuEl = null;
let menuEntries = [];
let menuIdx = 0;

// ---- slash commands ----
// Each entry: { label, arg, desc, complete, exec }. `exec` missing means the
// entry only completes text (e.g. "/mode " opens the mode sub-entries).

const COMMANDS = [
  {
    name: "/compact", desc: "Compress the conversation into a summary",
    exec: () => actions.compact(),
  },
  {
    name: "/clear", desc: "Start a new conversation",
    exec: () => hooks.onNewConversation(),
  },
  {
    name: "/mode", arg: "<plan|ask|auto-edit|dontask|yolo>",
    desc: "Switch the permission mode",
    complete: "/mode ",
    // no exec: Tab/Enter completes to "/mode " and lists the modes
    fallback: () => openModeMenu($("mode-pill")),
  },
  {
    name: "/model", desc: "Pick the model for this session",
    exec: () => openModelMenu($("model-pill")),
  },
  {
    name: "/help", desc: "Keyboard shortcuts and slash commands",
    exec: () => openHelp(),
  },
];

// Entries the menu should show for the current input text, or null for
// "no menu" (the text is not a bare slash command).
function entriesFor(text) {
  if (!text.startsWith("/")) return null;

  const modeMatch = /^\/mode\s+(.*)$/.exec(text);
  if (modeMatch) {
    const q = modeMatch[1].trim().toLowerCase();
    return MODE_DESCS
      .filter(([id]) => id.startsWith(q))
      .map(([id, desc]) => ({
        label: "/mode " + id, desc, complete: "/mode " + id,
        exec: () => actions.setMode(id),
      }));
  }

  if (/\s/.test(text)) return null;   // an argument for something else — no menu
  const q = text.toLowerCase();
  return COMMANDS
    .filter((c) => c.name.startsWith(q))
    .map((c) => ({
      label: c.name, arg: c.arg, desc: c.desc,
      complete: c.complete || c.name, exec: c.exec,
    }));
}

// Run a fully typed slash command. Returns true when it was handled.
function runSlash(text) {
  const parts = text.split(/\s+/);
  const c = COMMANDS.find((x) => x.name === parts[0]);
  if (!c) return false;
  const arg = parts.slice(1).join(" ").trim();
  if (c.name === "/mode") {
    if (arg) actions.setMode(arg); else c.fallback();
    return true;
  }
  if (!c.exec) return false;
  c.exec();
  return true;
}

// ---- slash menu ----

function slashOpen() { return !!menuEl && menuEl.isConnected; }

function hideSlash() {
  if (menuEl) menuEl.remove();
  menuEl = null;
  menuEntries = [];
  menuIdx = 0;
}

function renderItems() {
  return menuEntries.map((e, i) => `
    <button class="menu-item${i === menuIdx ? " hover" : ""}" data-idx="${i}">
      <div class="sm-line"><span class="sm-cmd">${esc(e.label)}</span>${
        e.arg ? `<span class="sm-arg">${esc(e.arg)}</span>` : ""}</div>
      <div class="sm-desc">${esc(e.desc)}</div>
    </button>`).join("");
}

function paintSelection() {
  if (!menuEl) return;
  menuEl.querySelectorAll(".menu-item").forEach((b, i) => {
    b.classList.toggle("hover", i === menuIdx);
    if (i === menuIdx) b.scrollIntoView({ block: "nearest" });
  });
}

function refreshSlash() {
  const entries = entriesFor(input.value);
  if (!entries || !entries.length) { hideSlash(); return; }

  const keepLabel = menuEntries[menuIdx]?.label;
  menuEntries = entries;
  const keep = entries.findIndex((e) => e.label === keepLabel);
  menuIdx = keep >= 0 ? keep : 0;

  if (!slashOpen()) {
    // Other menus (mode / model) remove themselves the same way.
    document.querySelectorAll(".menu").forEach((m) => m.remove());
    menuEl = document.createElement("div");
    menuEl.className = "menu slash-menu";
    document.body.appendChild(menuEl);
    menuEl.addEventListener("mousedown", (e) => {
      const b = e.target.closest("[data-idx]");
      if (!b) return;
      e.preventDefault();           // keep focus in the textarea
      menuIdx = Number(b.dataset.idx);
      activate(false);
    });
  }
  menuEl.innerHTML = `<div class="menu-list">${renderItems()}</div>
    <div class="slash-hint">↑↓ select · Tab complete · Enter run · Esc close</div>`;
  position();
}

function position() {
  const box = document.querySelector(".composer-box");
  if (!box || !menuEl) return;
  const r = box.getBoundingClientRect();
  menuEl.style.left = r.left + "px";
  menuEl.style.width = r.width + "px";
  menuEl.style.top = Math.max(8, r.top - menuEl.offsetHeight - 8) + "px";
}

// Enter / Tab / click on a menu entry. `preferComplete` is Tab's behavior:
// fill the text in first, execute only once it is fully typed.
function activate(preferComplete) {
  const e = menuEntries[menuIdx];
  if (!e) return;
  const typed = input.value.trim();
  const canExec = !!e.exec && (!preferComplete || typed === e.complete.trim());
  if (canExec) {
    hideSlash();          // before exec: /model and /mode open their own menu
    setInput("");
    e.exec();
    return;
  }
  setInput(e.complete);
  refreshSlash();
}

// ---- input helpers ----

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
}

function setInput(text) {
  input.value = text;
  input.selectionStart = input.selectionEnd = text.length;
  autosize();
}

function remember(text) {
  syncHistory();
  if (history[history.length - 1] !== text) {
    history.push(text);
    if (history.length > HISTORY_MAX) history = history.slice(-HISTORY_MAX);
    saveHistory(history);
  }
  histIdx = null;
  draft = "";
}

function onFirstLine() {
  return !input.value.slice(0, input.selectionStart).includes("\n");
}

function onLastLine() {
  return !input.value.slice(input.selectionEnd).includes("\n");
}

function historyBack() {
  syncHistory();
  if (!history.length) return false;
  if (histIdx === null) {
    draft = input.value;
    histIdx = history.length - 1;
  } else if (histIdx > 0) {
    histIdx--;
  }
  setInput(history[histIdx]);
  return true;
}

function historyForward() {
  if (histIdx === null) return false;
  if (histIdx < history.length - 1) {
    histIdx++;
    setInput(history[histIdx]);
  } else {
    histIdx = null;
    setInput(draft);
    draft = "";
  }
  return true;
}

// ---- send ----

function send() {
  const text = input.value.trim();
  if (!text) return;
  hideSlash();
  remember(text);
  if (text.startsWith("/") && runSlash(text)) { setInput(""); return; }
  actions.userMessage(text);
  setInput("");
}

// ---- wiring ----

export function initComposer(h) {
  hooks = { onNewConversation: () => {}, ...(h || {}) };
  input = $("input");

  input.addEventListener("input", () => {
    histIdx = null;               // typing leaves history browsing
    autosize();
    refreshSlash();
  });

  input.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    if (slashOpen()) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        menuIdx = (menuIdx + 1) % menuEntries.length;
        paintSelection();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        menuIdx = (menuIdx - 1 + menuEntries.length) % menuEntries.length;
        paintSelection();
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        if (e.key === "Enter" && e.shiftKey) return;   // newline still works
        e.preventDefault();
        activate(e.key === "Tab");
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();      // closing the menu must not interrupt
        hideSlash();
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); return; }
    if (e.key === "ArrowUp" && !e.shiftKey && onFirstLine()) {
      if (historyBack()) e.preventDefault();
      return;
    }
    if (e.key === "ArrowDown" && !e.shiftKey && onLastLine()) {
      if (historyForward()) e.preventDefault();
    }
  });

  input.addEventListener("blur", () => setTimeout(hideSlash, 0));

  $("btn-send").addEventListener("click", send);
  $("btn-interrupt").addEventListener("click", () => actions.interrupt());
  $("btn-compact").addEventListener("click", () => actions.compact());
  $("mode-pill").addEventListener("click", (e) => openModeMenu(e.currentTarget));
  $("model-pill").addEventListener("click", (e) => openModelMenu(e.currentTarget));
  $("btn-help")?.addEventListener("click", () => openHelp());

  window.addEventListener("resize", () => { if (slashOpen()) position(); });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.querySelector(".modal-backdrop, .menu")) {
      actions.interrupt();
    }
  });
}
