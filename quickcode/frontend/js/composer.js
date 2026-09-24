// Composer: the message input and everything that hangs off it — autosize,
// drafts, send / interrupt / compact, the mode and model pills, and the one
// completion menu that slash commands and @paths share. The pieces with state
// of their own live beside it: composer/pills.js (composition and profile),
// composer/slash.js (the commands), composer/paths.js (@ completion) and
// composer/history.js (↑/↓ recall).

import { currentProject } from "./api.js";
import { historyBack, historyForward, remember, resetWalk } from "./composer/history.js";
import { atToken, initPaths, insertPath, pathEntries } from "./composer/paths.js";
import { mountPills, placePillMenus } from "./composer/pills.js";
import { entriesFor, initSlash, runSlash } from "./composer/slash.js";
import { openHelp } from "./help/quickref.js";
import { openModeMenu, openModelMenu } from "./menus.js";
import { store, subscribe } from "./store.js";
import { esc } from "./util.js";
import { actions } from "./ws.js";

export { refreshCompositionPill, refreshProfilePill } from "./composer/pills.js";

const $ = (id) => document.getElementById(id);

let input = null;
let menuEl = null;
let menuEntries = [];
let menuIdx = 0;
let menuHint = "";

// ---- the completion menu: slash commands and @paths ----

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
        e.arg ? `<span class="sm-arg">${esc(e.arg)}</span>` : ""}</div>${
      e.desc ? `<div class="sm-desc">${esc(e.desc)}</div>` : ""}
    </button>`).join("");
}

function paintSelection() {
  if (!menuEl) return;
  menuEl.querySelectorAll(".menu-item").forEach((b, i) => {
    b.classList.toggle("hover", i === menuIdx);
    if (i === menuIdx) b.scrollIntoView({ block: "nearest" });
  });
}

const SLASH_HINT = "↑↓ select · Tab complete · Enter run · Esc close";
const PATH_HINT = "↑↓ select · Tab / Enter insert · Esc close";

// One menu, two sources: a slash command (keyed off the whole value, because
// that is what a slash command is) or the @ token under the caret.
function currentEntries() {
  const slash = entriesFor(input.value);
  if (slash) { menuHint = SLASH_HINT; return slash; }
  const tok = atToken(input);
  if (!tok) return null;
  menuHint = PATH_HINT;
  return pathEntries(tok.query, insertPathEntry);
}

// A directory keeps the menu open on its contents, which is what makes walking
// down a tree feel like one gesture.
function insertPathEntry(entry) {
  if (!insertPath(input, entry)) { hideSlash(); return; }
  autosize();
  if (entry.is_dir) refreshSlash(); else hideSlash();
}

function refreshSlash() {
  const entries = currentEntries();
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
    <div class="slash-hint">${esc(menuHint)}</div>`;
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
  // A path entry rewrites one token rather than the whole value, so it owns
  // its own insertion; Tab and Enter mean the same thing for it.
  if (e.insert) { e.insert(); return; }
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
  saveDraft();
}

let draftKey = null;
function saveDraft() {
  if (!draftKey || !input) return;
  try {
    if (input.value) sessionStorage.setItem(draftKey, input.value);
    else sessionStorage.removeItem(draftKey);
  } catch { /* retain the draft in the mounted composer */ }
}

function restoreDraft() {
  if (!store.convId) return;
  const paneId = new URLSearchParams(location.search).get("view");
  const key = `qc-draft:${currentProject()}:${paneId || store.convId}`;
  if (key === draftKey) return; // Reconnect must not overwrite active typing.
  saveDraft();
  draftKey = key;
  try { input.value = sessionStorage.getItem(key) || ""; } catch { input.value = ""; }
  autosize();
}

function onFirstLine() {
  return !input.value.slice(0, input.selectionStart).includes("\n");
}

function onLastLine() {
  return !input.value.slice(input.selectionEnd).includes("\n");
}

// ---- send ----

function send() {
  const text = input.value.trim();
  if (!text) return;
  hideSlash();
  // A bare slash command is a button that happens to be typed; it is not a
  // message, and it does not belong in the recall of things you said. Text
  // that merely starts with "/" and matches no command still does.
  if (text.startsWith("/")) {
    const ran = runSlash(text);
    if (ran === "refused") return;                    // ws.js said why
    if (ran) { resetWalk(); setInput(""); return; }
  }
  // The box is cleared only once the socket has actually taken the message.
  // It used to be cleared either way, so a send over a dead connection deleted
  // what the user had written and told them nothing — the one bug here that
  // costs someone work rather than a click. ws.js owns the sentence that says
  // what happened; this only has to hold on to the words.
  if (!actions.userMessage(text)) return;
  remember(text);
  setInput("");
}

// ---- wiring ----

export function initComposer(h) {
  initSlash(h);
  input = $("input");
  initPaths({ refresh: () => { if (atToken(input)) refreshSlash(); } });
  mountPills();

  input.addEventListener("input", () => {
    saveDraft();
    resetWalk();                  // typing leaves history browsing
    autosize();
    refreshSlash();
  });

  input.addEventListener("keydown", (e) => {
    if (composing(e)) return;
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
      const recalled = historyBack(input.value);
      if (recalled !== null) { setInput(recalled); e.preventDefault(); }
      return;
    }
    if (e.key === "ArrowDown" && !e.shiftKey && onLastLine()) {
      const recalled = historyForward();
      if (recalled !== null) { setInput(recalled); e.preventDefault(); }
    }
  });

  input.addEventListener("blur", () => setTimeout(hideSlash, 0));

  $("btn-send").addEventListener("click", send);
  subscribe((kind) => { if (kind === "reset" || kind === "state") restoreDraft(); });
  window.addEventListener("pagehide", saveDraft);
  $("btn-interrupt").addEventListener("click", () => actions.interrupt());
  $("btn-compact").addEventListener("click", () => actions.compact());
  $("mode-pill").addEventListener("click", (e) => openModeMenu(e.currentTarget));
  $("model-pill").addEventListener("click", (e) => openModelMenu(e.currentTarget));
  $("btn-help")?.addEventListener("click", () => openHelp());

  window.addEventListener("resize", () => {
    if (slashOpen()) position();
    placePillMenus();
  });

  document.addEventListener("keydown", (e) => {
    // Escape during an IME composition cancels the candidate; it is not
    // addressed to the agent, and a CJK typist would stop every turn with it.
    if (e.key === "Escape" && !composing(e) && escInterrupts()) actions.interrupt();
  });
}

// A keystroke that belongs to an input method rather than to the page. WebKit —
// the engine behind the app window on macOS and Linux — fires the Enter that
// commits a candidate with `isComposing` already false, and only keyCode 229
// gives it away; without that check the half-typed message was sent.
function composing(e) {
  return e.isComposing || e.keyCode === 229;
}

// Escape interrupts the turn — but only when it is not already spoken for.
// A modal (permission, plan review) or an open menu closes on Escape instead,
// and during a permission prompt that is exactly the case. Exported because
// the activity line prints "esc to interrupt" and must not promise a key that
// currently does something else.
export function escInterrupts() {
  return !document.querySelector(".modal-backdrop, .menu");
}
