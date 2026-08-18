// Composer: the message input and everything that hangs off it — autosize,
// send / interrupt / compact, mode + model pills, input history (localStorage)
// and the slash-command menu. Parity with the old Textual TUI composer.

import { api, currentProject } from "./api.js";
import { openHelp, openModeMenu, openModelMenu } from "./modals.js";
import { store } from "./store.js";
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

// ---- the composition pill -------------------------------------------------
//
// Three things are session-scoped and belong next to the send button: the mode,
// the model, and now the composition. Everything else — authoring a plugin,
// editing an agent, changing what a composition *means* — takes effect in the
// next session and lives in the configuration view. A setting whose blast
// radius is every future session does not belong on a pill.
//
// The rule the switch follows is the plan's, and it is a refusal rather than a
// queue: the model has already been told what tools it has, so a switch is
// taken at a turn boundary or not at all. A switch that lands invisibly three
// seconds later is worse than one that does not happen.

const GAP = 6;

let compositionPill = null;
let compMenuEl = null;

function compositionState() {
  return store.state?.composition || null;
}

const RUNNING = new Set(["sending", "streaming", "executing_tools"]);

// The `state` event is emitted at turn boundaries, so `busy` on it lags a turn
// that is under way; the status stream does not. Both are only a hint: the
// server re-checks and answers 409 with the reason, and that answer is the
// authority.
function blockedReason() {
  const c = compositionState();
  if (RUNNING.has(store.agentStatus)) {
    return "the agent is running — a composition switch takes effect at a turn "
      + "boundary, so it is refused rather than queued";
  }
  return c && !c.switchable ? c.blocked_reason : "";
}

export function refreshCompositionPill() {
  if (!compositionPill) return;
  const c = compositionState();
  if (!c) {
    compositionPill.classList.add("hidden");
    return;
  }
  const blocked = blockedReason();
  compositionPill.classList.remove("hidden");
  compositionPill.textContent = `${c.id || "standard"} ▾`;
  compositionPill.classList.toggle("blocked", !!blocked);
  compositionPill.title = blocked
    ? `Cannot switch right now: ${blocked}`
    : `Composition: ${c.tools} tools · ceiling ${c.ceiling}`
      + (c.spawns?.length ? ` · spawns ${c.spawns.join(", ")}` : " · no delegation")
      + "\nSwitching applies at a turn boundary.";
}

function closeCompMenu() {
  if (compMenuEl) { compMenuEl.remove(); compMenuEl = null; }
}

function placeMenu(m, anchor) {
  const r = anchor.getBoundingClientRect();
  m.style.maxHeight = Math.max(160, Math.min(window.innerHeight * 0.6,
    r.top - GAP * 2)) + "px";
  m.style.bottom = window.innerHeight - r.top + GAP + "px";
  m.style.top = "auto";
  m.style.left = Math.max(GAP,
    Math.min(r.left, window.innerWidth - m.offsetWidth - 12)) + "px";
}

async function openCompositionMenu(anchor) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const current = compositionState();
  let payload = null;
  try { payload = await api.presets(); } catch { /* offline: an empty list */ }
  const presets = payload?.presets || [];

  const rows = presets.map((p) => `
    <button class="menu-item" data-preset="${esc(p.id)}">
      <div class="mi-title">${esc(p.title)}${
        p.id === current?.id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-desc">${esc(p.description || "")}</div>
    </button>`).join("");

  const blocked = blockedReason();
  const m = document.createElement("div");
  m.className = "menu comp-menu";
  m.innerHTML = `
    <div class="menu-head">Composition for this session</div>
    ${blocked ? `<div class="menu-blocked">Refused right now — ${esc(blocked)}.
      It is not queued: the model has already been told what tools it has, so a
      switch is taken between turns or not at all.</div>` : ""}
    <div class="menu-list">${rows}</div>
    <button class="menu-item comp-custom" data-customise>
      <div class="mi-title">Customise this…</div>
      <div class="mi-desc">Duplicate the active composition into one you own and
        open it in the workbench.</div>
    </button>
    <div class="comp-note" data-comp-note>Switching re-resolves the tools, the
      prompt and the ceiling, records it in the session log and marks the
      transcript. The next turn pays one uncached input.</div>`;
  document.body.appendChild(m);
  compMenuEl = m;
  placeMenu(m, anchor);

  const dismiss = (e) => {
    if (!m.isConnected) { document.removeEventListener("mousedown", dismiss, true); return; }
    if (!m.contains(e.target)) { closeCompMenu(); document.removeEventListener("mousedown", dismiss, true); }
  };
  setTimeout(() => document.addEventListener("mousedown", dismiss, true), 0);

  m.addEventListener("click", async (e) => {
    const note = m.querySelector("[data-comp-note]");
    if (e.target.closest("[data-customise]")) {
      closeCompMenu();
      try {
        const made = await api.deriveComposition(current?.id || "standard");
        location.hash = `#/config/agents/%40orchestrator?preset=${
          encodeURIComponent(made.id)}`;
      } catch (err) {
        window.alert(`Could not duplicate the composition: ${err.message}`);
      }
      return;
    }
    const btn = e.target.closest("[data-preset]");
    if (!btn) return;
    note.textContent = "Switching…";
    try {
      await api.switchComposition(store.convId, btn.dataset.preset);
      closeCompMenu();
    } catch (err) {
      // The server's own words. A 409 here is the reason, and it is the most
      // useful sentence on the screen.
      note.textContent = String(err.message).replace(/^\d+:\s*/, "");
      note.classList.add("is-err");
    }
  });
}

function mountCompositionPill() {
  const left = document.querySelector(".composer-left");
  if (!left || compositionPill) return;
  compositionPill = document.createElement("button");
  compositionPill.id = "composition-pill";
  compositionPill.className = "pill hidden";
  compositionPill.title = "Composition";
  compositionPill.textContent = "standard ▾";
  // Beside mode and model, because those are the other two things a person
  // changes mid-conversation.
  left.insertBefore(compositionPill, left.children[2] || null);
  compositionPill.addEventListener("click", (e) => openCompositionMenu(e.currentTarget));
}

// ---- the permission-profile pill ------------------------------------------
//
// The fourth session-scoped control, and the one the mode pill has always
// implied: the mode says how much this session asks about in general, a profile
// says the same thing at the granularity of a single rule. It sits immediately
// beside the mode pill because the two are one question read at two
// resolutions.
//
// Unlike a composition switch this is never refused and never waits for a turn
// boundary. Nothing the model has been told depends on which of its tools will
// prompt, so the server rewrites the running engine and answers with what it
// changed; `Conversation.apply_posture` argues the same case from the other
// side. A posture you had to reopen the session to change would be a settings
// file with a nicer font.

let profilePill = null;
let profMenuEl = null;
// `undefined` = never read, `null` = read and failed. The distinction is what
// stops a failed fetch from being retried on every repaint.
let profileList;
let unnamedId = "";      // an active id the last read could not put a title to

async function loadProfiles(force = false) {
  if (profileList !== undefined && !force) return profileList;
  try { profileList = await api.profiles(); } catch { profileList = null; }
  return profileList;
}

function profileById(id) {
  return (profileList?.profiles || []).find((p) => p.id === id) || null;
}

export function refreshProfilePill() {
  if (!profilePill) return;
  // Hidden until there is a session, like the composition pill: a posture is a
  // fact about a conversation, not about the window.
  if (!store.state) { profilePill.classList.add("hidden"); return; }
  profilePill.classList.remove("hidden");

  const id = store.state.profile || "";
  const p = profileById(id);
  profilePill.textContent = `§ ${p?.title || id || "no profile"} ▾`;
  profilePill.classList.toggle("subtle", !id);
  profilePill.title = id
    ? `Permission profile: ${p?.title || id}${p?.description ? `\n${p.description}` : ""}`
      + "\nIts rules add to this project's own; its mode is where the session"
      + " started, not a ceiling."
    : "No permission profile — this project's own rules apply on their own.";

  // The pill knows the id from the session and the title only from the list, so
  // an id it cannot name asks for the list — once per id, not once per repaint.
  if (id && !p && unnamedId !== id) {
    unnamedId = id;
    loadProfiles(true).then(refreshProfilePill);
  }
}

function closeProfMenu() {
  if (profMenuEl) { profMenuEl.remove(); profMenuEl = null; }
}

function profileRowHtml(id, title, desc, layer, active) {
  return `<button class="menu-item" data-profile="${esc(id)}">
      <div class="mi-title">${esc(title)}${
        id === active ? '<span class="check">✓</span>' : ""}${
        layer ? `<span class="pf-layer" data-layer="${esc(layer)}">${
          esc(layer)}</span>` : ""}</div>
      <div class="mi-desc">${esc(desc)}</div>
    </button>`;
}

async function openProfileMenu(anchor) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  // Re-read rather than reuse: a profile written on the configuration page a
  // moment ago has to be in this list, or the two screens disagree.
  const data = await loadProfiles(true);
  const list = data?.profiles || [];
  const active = data?.active ?? (store.state?.profile || "");

  const m = document.createElement("div");
  m.className = "menu prof-menu";
  m.innerHTML = `
    <div class="menu-head">Permission profile for this session</div>
    <div class="menu-list">
      ${profileRowHtml("", "No profile",
        "This project's own rules, on their own.", "", active)}
      ${list.map((p) => profileRowHtml(p.id, p.title,
        // A profile the trust gate reduced says so here too. The list is where
        // it is picked, so it is where "this does less than it says" belongs.
        ((p.refused || []).length
          ? `Reduced — this project is not trusted, so its ${
              p.refused.join(" and ")} was ignored. ` : "")
        + (p.description || ""),
        p.layer, active)).join("")}
    </div>
    <a class="menu-item prof-manage" href="#/config/profiles">
      <div class="mi-title">Manage profiles…</div>
      <div class="mi-desc">Write one of your own — allow <code>bash(git **)</code>,
        deny <code>read(**)</code>, whatever this piece of work needs.</div>
    </a>
    <div class="prof-note" data-prof-note>A profile's rules are added to this
      project's own rather than replacing them, so it narrows by denying; its
      mode is where a session starts, and Shift+Tab still works afterwards.
      Switching applies straight away, to every session open on this project.</div>`;
  document.body.appendChild(m);
  profMenuEl = m;
  placeMenu(m, anchor);

  const dismiss = (e) => {
    if (!m.isConnected) { document.removeEventListener("mousedown", dismiss, true); return; }
    if (!m.contains(e.target)) { closeProfMenu(); document.removeEventListener("mousedown", dismiss, true); }
  };
  setTimeout(() => document.addEventListener("mousedown", dismiss, true), 0);

  m.addEventListener("click", async (e) => {
    if (e.target.closest(".prof-manage")) { closeProfMenu(); return; }
    const btn = e.target.closest("[data-profile]");
    if (!btn) return;
    const note = m.querySelector("[data-prof-note]");
    note.textContent = "Switching…";
    try {
      const res = await api.setActiveProfile(btn.dataset.profile);
      profileList = res;              // the write answers with the whole list
      closeProfMenu();
      refreshProfilePill();
    } catch (err) {
      note.textContent = String(err.message).replace(/^\d+:\s*/, "");
      note.classList.add("is-err");
    }
  });
}

function mountProfilePill() {
  const left = document.querySelector(".composer-left");
  const mode = $("mode-pill");
  if (!left || !mode || profilePill) return;
  profilePill = document.createElement("button");
  profilePill.id = "profile-pill";
  profilePill.className = "pill subtle hidden";
  profilePill.textContent = "§ no profile ▾";
  left.insertBefore(profilePill, mode.nextSibling);
  profilePill.addEventListener("click", (e) => openProfileMenu(e.currentTarget));
}

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
    name: "/composition", desc: "Switch this session's composition (at a turn boundary)",
    exec: () => openCompositionMenu($("composition-pill") || $("model-pill")),
  },
  {
    name: "/profile", desc: "Switch this session's permission profile (takes effect now)",
    exec: () => openProfileMenu($("profile-pill") || $("mode-pill")),
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
  mountCompositionPill();
  mountProfilePill();

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

  window.addEventListener("resize", () => {
    if (slashOpen()) position();
    if (compMenuEl && compositionPill) placeMenu(compMenuEl, compositionPill);
    if (profMenuEl && profilePill) placeMenu(profMenuEl, profilePill);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && escInterrupts()) actions.interrupt();
  });
}

// Escape interrupts the turn — but only when it is not already spoken for.
// A modal (permission, plan review) or an open menu closes on Escape instead,
// and during a permission prompt that is exactly the case. Exported because
// the activity line prints "esc to interrupt" and must not promise a key that
// currently does something else.
export function escInterrupts() {
  return !document.querySelector(".modal-backdrop, .menu");
}
