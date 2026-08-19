// Composer: the message input and everything that hangs off it — autosize,
// send / interrupt / compact, mode + model pills, input history (localStorage)
// and the slash-command menu. Parity with the old Textual TUI composer.

import { api, currentProject } from "./api.js";
import { openHelp, openModeMenu, openModelMenu } from "./modals.js";
import { store } from "./store.js";
import { toast, toastError } from "./toast.js";
import { debounce, esc } from "./util.js";
import { actions } from "./ws.js";

const $ = (id) => document.getElementById(id);

const HISTORY_KEY = "qc-history";
const HISTORY_MAX = 100;
// v1 was a bare string[]. v2 is {v, items} — same strings, but a shape that can
// grow. The bump matters because v1's reader hard-filtered to strings and would
// have silently eaten anything else; v2 reads v1 and rewrites it, and a v1
// reader handed v2 sees "no history" rather than a crash.
const HISTORY_VERSION = 2;
const PATH_LIMIT = 40;

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
        // The menu it was raised from is already gone, so there is no inline
        // home for this sentence — which is exactly the toast's case.
        toastError(`Could not duplicate the composition: ${err.message}`);
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
    const raw = JSON.parse(localStorage.getItem(historyKey()) || "null");
    // A v1 list is still perfectly good data: it is read as-is and rewritten in
    // the new shape by the next send, rather than thrown away.
    const items = Array.isArray(raw) ? raw : (raw?.v >= 2 ? raw.items : []);
    return (Array.isArray(items) ? items : []).filter((x) => typeof x === "string" && x);
  } catch { return []; }
}

function saveHistory(h) {
  try {
    localStorage.setItem(historyKey(), JSON.stringify({ v: HISTORY_VERSION, items: h }));
  } catch { /* quota / private mode */ }
}

// The composer is wired once, before any project is open, so the list is
// (re)read the first time each project needs it.
function syncHistory() {
  const pid = currentProject() || "default";
  if (pid === historyPid) return;
  historyPid = pid;
  history = loadHistory();
  resetWalk();
}

// ---- module state ----

let input = null;
let hooks = { onNewConversation: () => {} };
let history = [];
let historyPid = null;
// The recall walk. `walk` is null while the draft is being edited; once ↑ is
// pressed it holds the entries that match what was already typed, and it is
// that filtered list — not the whole history — that ↑/↓ move through.
let walk = null;
let walkIdx = 0;
let draft = "";
let menuEl = null;
let menuEntries = [];
let menuIdx = 0;
let menuHint = "";

// ---- /init: the project-instructions file, written by the agent ----
//
// `_load_project_instructions` (quickcode/config.py) reads the first of
// QUICKCODE.md, AGENTS.md, CLAUDE.md it finds at the project root and injects
// it verbatim into every prompt. There is deliberately no endpoint that writes
// a file at the project root, and this command does not add one: it sends a
// message. The agent surveys the repository with the tools it already has and
// writes the file with `write`, which means the permission engine sees the
// write like any other — a slash command that could put a file on disk without
// passing the gate would be a hole in the gate.

const INSTRUCTION_FILES = ["QUICKCODE.md", "AGENTS.md", "CLAUDE.md"];

const INIT_PROMPT = `Write a QUICKCODE.md at the root of this project.

QuickCode injects that file verbatim into the system prompt of every session \
here, so it is read by someone who has never seen this repository and is about \
to change it.

Survey before you write. Read the dependency and build manifests, the test \
configuration, the CI workflow, the README, and enough of the source tree to \
see how it is actually laid out. Where the documentation and the code \
disagree, the code is right.

Then write the file with the write tool. Cover:

- Build, run and test: the exact commands, copied from the manifests or CI \
rather than guessed, and which one to run to check a change.
- Architecture: the few directories or modules that matter, what each is \
responsible for, and where a request or a command actually enters.
- Conventions this repository keeps that a newcomer would otherwise break — \
naming, formatting, imports, error handling, test style, commit style.
- Anything genuinely surprising: a rule you had to read the source to discover.

Keep it short. One screen, ideally under 60 lines — it is a prompt paid for on \
every turn, not documentation. No preamble, no history, no praise for the \
project. Leave a section out rather than filling it with a plausible guess, \
and say at the end what you left out and why.`;

const initUpdatePrompt = (name) => `This project already has a ${name} at its \
root, and QuickCode reads it into the prompt of every session here. Update it \
— do not overwrite it.

Read it first, then check it against the repository as it is now: try the \
build and test commands it names, look at the directories it describes, and \
confirm the conventions it claims. Find what has gone stale and what is \
missing.

Then edit it in place, keeping its structure and its voice, and tell me what \
you changed and why. Leave every line that is still accurate alone. It should \
stay short — one screen — and it should still cover the build, run and test \
commands, the architecture worth knowing, and the conventions a newcomer would \
otherwise break.`;

// Which of the three names is actually on disk. Asked one name at a time so
// the answer is exact: a root listing can be capped, and "is QUICKCODE.md
// there" must not depend on how many files sort ahead of it.
async function existingInstructions() {
  const found = [];
  for (const name of INSTRUCTION_FILES) {
    try {
      const res = await api.paths(name, 5);
      if ((res.paths || []).some((p) => !p.is_dir && p.path.toLowerCase() === name.toLowerCase())) {
        found.push(name);
      }
    } catch { /* offline or no project: fall through to the write prompt */ }
  }
  return found;
}

async function runInit() {
  const found = await existingInstructions();
  if (found.length) {
    toast(`${found[0]} already exists — asking the agent to update it rather `
      + "than replace it.", { kind: "info" });
    actions.userMessage(initUpdatePrompt(found[0]));
    return;
  }
  actions.userMessage(INIT_PROMPT);
}

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
    name: "/init", desc: "Have the agent survey this project and write QUICKCODE.md",
    exec: () => { runInit(); },
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

// ---- @path completion ----
//
// The slash menu keys off the whole input value, because a slash command *is*
// the whole message. A path is not: it is one token inside a sentence, so this
// half keys off the token under the caret instead, and it reuses the same menu
// — render, position, ↑↓, Tab/Enter, Escape — rather than growing a second
// popover with its own manners.

// The token being typed, when it is an @ token. An `@` that is not at the
// start of a token (an email address, a decorator argument) is not one.
function atToken() {
  if (!input) return null;
  const before = input.value.slice(0, input.selectionStart ?? input.value.length);
  const m = /(?:^|\s)@([^\s@]*)$/.exec(before);
  if (!m) return null;
  return { start: before.length - m[1].length - 1, end: before.length, query: m[1] };
}

// The last answer from the server, and the query it answered. Kept so that
// typing another character narrows what is already on screen instead of
// blanking the menu for the length of a round trip.
let pathCache = { query: null, entries: [] };
let pathWanted = null;

function pathRows(query) {
  const known = pathCache.query;
  if (known === null) return null;
  if (!query.toLowerCase().startsWith(known.toLowerCase())) {
    // Backspaced past what the cache covers: what is there is a subset of the
    // right answer, so show it and let the fetch widen it.
    return known.toLowerCase().startsWith(query.toLowerCase()) ? pathCache.entries : null;
  }
  const cut = query.lastIndexOf("/") + 1;
  const head = query.slice(0, cut).toLowerCase();
  const needle = query.slice(cut).toLowerCase();
  return pathCache.entries.filter((p) => {
    const low = p.path.toLowerCase();
    if (!low.startsWith(head)) return false;
    const rest = low.slice(head.length);
    return rest.startsWith(needle) || rest.split("/").pop().includes(needle);
  });
}

const fetchPaths = debounce(async (query) => {
  if (pathWanted !== query) return;          // outrun by a later keystroke
  let entries = [];
  try {
    const res = await api.paths(query, PATH_LIMIT);
    entries = res.paths || [];
  } catch { /* no project, or the query escaped: an empty menu is the answer */ }
  if (pathWanted !== query) return;
  pathCache = { query, entries };
  if (atToken()) refreshSlash();
}, 120);

// Replace the @ token with the chosen path. A directory keeps the menu open on
// its contents, which is what makes walking down a tree feel like one gesture.
function insertPath(entry) {
  const tok = atToken();
  if (!tok) { hideSlash(); return; }
  const text = "@" + entry.path + (entry.is_dir ? "/" : " ");
  const v = input.value;
  input.value = v.slice(0, tok.start) + text + v.slice(tok.end);
  input.selectionStart = input.selectionEnd = tok.start + text.length;
  autosize();
  if (entry.is_dir) refreshSlash(); else hideSlash();
}

function pathEntriesFor(query) {
  if (pathWanted !== query && pathCache.query !== query) {
    pathWanted = query;
    fetchPaths(query);
  }
  const rows = pathRows(query);
  if (!rows) return null;
  return rows.slice(0, PATH_LIMIT).map((p) => ({
    label: p.path + (p.is_dir ? "/" : ""),
    arg: p.is_dir ? "dir" : "",
    desc: "",
    insert: () => insertPath(p),
  }));
}

// Run a fully typed slash command. Three answers, not two: `false` means the
// text is not a command at all (so it goes as a message), `"refused"` means it
// is one but the socket could not carry it — and the composer keeps the text
// rather than clearing a box over a command that never happened — and `true`
// means it ran. `actions.*` return false exactly when the send was refused, so
// the distinction costs nothing to propagate.
function runSlash(text) {
  const parts = text.split(/\s+/);
  const c = COMMANDS.find((x) => x.name === parts[0]);
  if (!c) return false;
  const arg = parts.slice(1).join(" ").trim();
  if (c.name === "/mode") {
    if (!arg) { c.fallback(); return true; }
    return actions.setMode(arg) || "refused";
  }
  if (!c.exec) return false;
  return c.exec() === false ? "refused" : true;
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
  const tok = atToken();
  if (!tok) return null;
  menuHint = PATH_HINT;
  return pathEntriesFor(tok.query);
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
}

function resetWalk() {
  walk = null;
  walkIdx = 0;
  draft = "";
}

function remember(text) {
  syncHistory();
  // Full dedupe, not just the adjacent one: the same command sent five times
  // over an afternoon should cost one line of recall, not five presses of ↑.
  const next = history.filter((h) => h !== text);
  next.push(text);
  history = next.length > HISTORY_MAX ? next.slice(-HISTORY_MAX) : next;
  saveHistory(history);
  resetWalk();
}

// What ↑ walks: the entries that start with what is already typed. An empty
// composer walks everything, which is the old behavior unchanged.
function matching(text) {
  const q = text.trim();
  if (!q) return history.slice();
  const low = q.toLowerCase();
  return history.filter((h) => h !== q && h.toLowerCase().startsWith(low));
}

function onFirstLine() {
  return !input.value.slice(0, input.selectionStart).includes("\n");
}

function onLastLine() {
  return !input.value.slice(input.selectionEnd).includes("\n");
}

function historyBack() {
  syncHistory();
  if (walk === null) {
    // The prefix is fixed when the walk starts and holds for the whole walk;
    // re-filtering on every press would use the recalled line as the filter.
    draft = input.value;
    walk = matching(draft);
    walkIdx = walk.length;
    if (!walk.length) { walk = null; return false; }
  }
  if (walkIdx > 0) walkIdx--;
  setInput(walk[walkIdx]);
  return true;
}

function historyForward() {
  if (walk === null) return false;
  if (walkIdx < walk.length - 1) {
    walkIdx++;
    setInput(walk[walkIdx]);
  } else {
    const back = draft;
    resetWalk();
    setInput(back);
  }
  return true;
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
  hooks = { onNewConversation: () => {}, ...(h || {}) };
  input = $("input");
  mountCompositionPill();
  mountProfilePill();

  input.addEventListener("input", () => {
    resetWalk();                  // typing leaves history browsing
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
