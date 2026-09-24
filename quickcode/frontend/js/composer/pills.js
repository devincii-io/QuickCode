// The composer's composition and permission-profile pills, and their menus.

import { api } from "../api.js";
import { store } from "../store.js";
import { toastError } from "../toast.js";
import { menuAt } from "../ui/menu.js";
import { esc } from "../util.js";

const $ = (id) => document.getElementById(id);

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

let compositionPill = null;

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

export async function openCompositionMenu(anchor) {
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
  const m = menuAt(anchor, rows, {
    className: "comp-menu",
    head: `<div class="menu-head">Composition for this session</div>
      ${blocked ? `<div class="menu-blocked">Refused right now — ${esc(blocked)}.
        It is not queued: the model has already been told what tools it has, so a
        switch is taken between turns or not at all.</div>` : ""}`,
    foot: `<button class="menu-item comp-custom" data-customise>
        <div class="mi-title">Customise this…</div>
        <div class="mi-desc">Duplicate the active composition into one you own and
          open it in the workbench.</div>
      </button>
      <div class="comp-note" data-comp-note>Switching re-resolves the tools, the
        prompt and the ceiling, records it in the session log and marks the
        transcript. The next turn pays one uncached input.</div>`,
  });

  m.addEventListener("click", async (e) => {
    const note = m.querySelector("[data-comp-note]");
    if (e.target.closest("[data-customise]")) {
      m.closeMenu();
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
      m.closeMenu();
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

function profileRowHtml(id, title, desc, layer, active) {
  return `<button class="menu-item" data-profile="${esc(id)}">
      <div class="mi-title">${esc(title)}${
        id === active ? '<span class="check">✓</span>' : ""}${
        layer ? `<span class="pf-layer" data-layer="${esc(layer)}">${
          esc(layer)}</span>` : ""}</div>
      <div class="mi-desc">${esc(desc)}</div>
    </button>`;
}

export async function openProfileMenu(anchor) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  // Re-read rather than reuse: a profile written on the configuration page a
  // moment ago has to be in this list, or the two screens disagree.
  const data = await loadProfiles(true);
  const list = data?.profiles || [];
  const active = data?.active ?? (store.state?.profile || "");

  const rows = profileRowHtml("", "No profile",
    "This project's own rules, on their own.", "", active)
    + list.map((p) => profileRowHtml(p.id, p.title,
      // A profile the trust gate reduced says so here too. The list is where
      // it is picked, so it is where "this does less than it says" belongs.
      ((p.refused || []).length
        ? `Reduced — this project is not trusted, so its ${
            p.refused.join(" and ")} was ignored. ` : "")
      + (p.description || ""),
      p.layer, active)).join("");
  const m = menuAt(anchor, rows, {
    className: "prof-menu",
    head: `<div class="menu-head">Permission profile for this session</div>`,
    foot: `<a class="menu-item prof-manage" href="#/config/profiles">
        <div class="mi-title">Manage profiles…</div>
        <div class="mi-desc">Write one of your own — allow <code>bash(git **)</code>,
          deny <code>read(**)</code>, whatever this piece of work needs.</div>
      </a>
      <div class="prof-note" data-prof-note>A profile's rules are added to this
        project's own rather than replacing them, so it narrows by denying; its
        mode is where a session starts, and the mode pill or /mode still changes
        it afterwards.
        Switching applies straight away, to every session open on this project.</div>`,
  });

  m.addEventListener("click", async (e) => {
    if (e.target.closest(".prof-manage")) { m.closeMenu(); return; }
    const btn = e.target.closest("[data-profile]");
    if (!btn) return;
    const note = m.querySelector("[data-prof-note]");
    note.textContent = "Switching…";
    try {
      const res = await api.setActiveProfile(btn.dataset.profile);
      profileList = res;              // the write answers with the whole list
      m.closeMenu();
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

export function mountPills() {
  mountCompositionPill();
  mountProfilePill();
}
