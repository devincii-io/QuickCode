// Modals and dropdown menus: permission review, plan review, mode menu,
// model picker, sessions, and quick settings.
//
// Configuration is no longer a dialog — it is #view-config, routed by
// `#/config/…` (js/config/). What stays a modal is the small set of
// install-level things people change mid-conversation without wanting to leave
// the chat: the provider endpoint, the API key and the theme. It is a shortcut
// into that view and says so.

import { api } from "./api.js";
import { KEYS, PANEL_NOTE, SLASH } from "./help/shortcuts.js";
import { sheetOpen } from "./settings/ui.js";
import { store, subscribe } from "./store.js";
import { toast, toastError, toastOk } from "./toast.js";
import { actions } from "./ws.js";
import { applyTheme, el, esc, fmtTokens, oneLine, relTime } from "./util.js";

const root = () => document.getElementById("modal-root");

// Escape has to close the dialog wherever the focus sits — clicking a settings
// tab leaves it on a button, not inside the modal — so the listener is
// document-level, added on open and dropped on close.
let modalEsc = null;

function closeModal() {
  if (modalEsc) {
    document.removeEventListener("keydown", modalEsc, true);
    modalEsc = null;
  }
  root().innerHTML = "";
  // Anything that opens a modal wipes the one on screen. If that was a review,
  // the agent is still blocked on it, so hand it back to the queue instead of
  // losing it — it reopens on top of whatever displaced it.
  reclaimReview();
}

// A review dialog is the agent waiting on an answer, so it does not close on
// Escape, the backdrop, or a ✕ — "no" is the Deny button, which the agent hears.
function modal(title, bodyHtml, footHtml = "", { dismissible = true } = {}) {
  closeModal();
  const m = el(`<div class="modal-backdrop"><div class="modal" tabindex="-1"
       role="dialog" aria-modal="true" aria-label="${esc(String(title))}">
    <div class="modal-head"><span>${title}</span>
      ${dismissible ? `<button class="ghost-btn" data-close>✕</button>` : ""}</div>
    <div class="modal-body">${bodyHtml}</div>
    ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
  </div></div>`);
  if (dismissible) {
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) closeModal();
    });
    modalEsc = (e) => {
      if (e.key !== "Escape") return;
      // A menu or a Settings sheet sits on top: those peel off first, one layer
      // per keystroke, instead of the dialog closing from underneath them.
      if (document.querySelector(".menu") || sheetOpen()) return;
      e.preventDefault();
      // Capture phase plus stopImmediatePropagation: the composer's Escape
      // (interrupt) and the panel's un-maximize must not also fire.
      e.stopImmediatePropagation();
      closeModal();
    };
    document.addEventListener("keydown", modalEsc, true);
  }
  root().appendChild(m);
  // Move focus into the dialog: Escape and Tab should belong to it from the
  // first keystroke, not to whatever button opened it.
  m.querySelector(".modal").focus();
  return m;
}

// ---- permission review ----
//
// Reviews are a QUEUE, not a single slot. Read-only tool calls in one assistant
// message run concurrently, so four of them can hit a protected path at once and
// the server opens four futures, each awaiting its own decision
// (server/manager.py: `await fut`). Showing them one dialog at a time and
// dropping the rest left those futures pending forever: the tool calls hung and
// the turn never ended. Every request is now either answered or still queued.

const queue = [];              // reviews waiting for an answer, oldest first
const queued = new Set();      // req_ids in `queue`, so replay cannot double-add
const decided = new Set();     // req_ids already answered from here
let current = null;            // the queue entry currently on screen

export function initReviews() {
  subscribe((kind, ev) => {
    if (kind === "reset") return resetReviews();
    if (kind === "event" && ev.type === "permission_request") enqueue("permission", ev);
    if (kind === "event" && ev.type === "plan_request") enqueue("plan", ev);
    if (kind === "state" && ev.pending) {
      // The authoritative list of what the server is still blocked on: it
      // recovers anything a reconnect or a lost frame would otherwise strand.
      for (const p of ev.pending) enqueue(p.kind, p);
    }
    if (kind === "event" && (ev.type === "permission_resolved" || ev.type === "plan_resolved")) {
      drop(ev.req_id);
    }
  });
}

function resetReviews() {
  queue.length = 0;
  queued.clear();
  decided.clear();
  current = null;
  closeModal();
}

function enqueue(kind, ev) {
  if (!ev?.req_id || queued.has(ev.req_id) || decided.has(ev.req_id)) return;
  // During replay only surface requests the server still reports as pending.
  if (store.replaying && !(store.state?.pending || []).some((p) => p.req_id === ev.req_id)) return;
  queue.push({ kind, ev });
  queued.add(ev.req_id);
  showNext();
  refreshWaiting();
}

// Answered, or resolved by something other than this dialog (another tab, an
// interrupt): forget it either way.
function drop(reqId) {
  const i = queue.findIndex((q) => q.ev.req_id === reqId);
  if (i >= 0) queue.splice(i, 1);
  queued.delete(reqId);
  if (current?.ev.req_id === reqId) {
    current = null;
    closeModal();
  }
  showNext();
  refreshWaiting();
}

// Something else opened a modal over a live review. The agent is still waiting,
// so the review goes back on screen once the displacing dialog has settled.
function reclaimReview() {
  if (!current) return;
  current = null;
  queueMicrotask(showNext);
}

function showNext() {
  if (current || !queue.length) return;
  const item = queue[0];   // stays queued while on screen; only a decision pops it
  if (item.kind === "permission") permissionModal(item.ev);
  else planModal(item.ev);
  current = item;
  refreshWaiting();
}

function answered(reqId) {
  decided.add(reqId);
  queued.delete(reqId);
  const i = queue.findIndex((q) => q.ev.req_id === reqId);
  if (i >= 0) queue.splice(i, 1);
  current = null;
  closeModal();
  showNext();
}

// "2 more waiting" is the difference between "the agent asked" and "the agent is
// blocked on four things"; with a fan-out of subagents that is the whole story.
// The node is always rendered and filled in afterwards: the requests that pile
// up behind this one arrive *after* its dialog is on screen.
const WAITING_NODE =
  `<div data-rv-waiting style="font-size:12px;color:var(--warning);margin-bottom:8px"></div>`;

function refreshWaiting() {
  const node = root().querySelector("[data-rv-waiting]");
  if (!node) return;
  const more = queue.length - 1;
  node.textContent = more > 0
    ? `${more} more request${more === 1 ? "" : "s"} waiting behind this one`
    : "";
}

function permissionModal(ev) {
  const m = modal(
    "Permission required",
    `${WAITING_NODE}
     <div>The agent wants to run
       <span class="perm-tool">${esc(ev.tool)}</span>
       ${ev.agent && ev.agent !== "main" ? `(subagent ${esc(ev.agent)})` : ""}</div>
     <div class="perm-preview">${esc(ev.preview || ev.arg)}</div>
     <div style="font-size:12px;color:var(--fg-dim)">Always-allow saves the rule
       <code>${esc(ev.rule_suggestion)}</code> to .quickcode/settings.local.json</div>
     <input class="deny-input hidden" placeholder="Why not? (optional — steers the agent)">`,
    `<button class="btn danger" data-act="deny">Deny</button>
     <button class="btn" data-act="always">Always allow</button>
     <button class="btn primary" data-act="allow">Allow once</button>`,
    { dismissible: false }
  );
  const denyInput = m.querySelector(".deny-input");
  m.querySelector(".modal-foot").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "deny" && denyInput.classList.contains("hidden")) {
      denyInput.classList.remove("hidden");
      denyInput.focus();
      e.target.textContent = "Confirm deny";
      return;
    }
    if (act === "allow") actions.permissionDecision(ev.req_id, true, false);
    if (act === "always") actions.permissionDecision(ev.req_id, true, true);
    if (act === "deny") actions.permissionDecision(ev.req_id, false, false, denyInput.value.trim());
    answered(ev.req_id);
  });
  return m;
}

function planModal(ev) {
  const m = modal(
    "Plan review",
    `${WAITING_NODE}
     <div class="perm-preview" style="max-height:52vh">${esc(ev.plan)}</div>
     <input class="deny-input hidden" placeholder="Feedback for the next iteration…">`,
    `<button class="btn" data-act="revise">Keep planning</button>
     <button class="btn" data-act="approve-ask">Approve · ask mode</button>
     <button class="btn primary" data-act="approve-auto">Approve · auto-edit</button>`,
    { dismissible: false }
  );
  const fb = m.querySelector(".deny-input");
  m.querySelector(".modal-foot").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "revise" && fb.classList.contains("hidden")) {
      fb.classList.remove("hidden"); fb.focus();
      e.target.textContent = "Send feedback";
      return;
    }
    if (act === "approve-ask") actions.planDecision(ev.req_id, true, "ask");
    if (act === "approve-auto") actions.planDecision(ev.req_id, true, "auto-edit");
    if (act === "revise") actions.planDecision(ev.req_id, false, null, fb.value.trim());
    answered(ev.req_id);
  });
  return m;
}

// ---- dropdown menus ----

const GAP = 8;

function menuAt(anchor, contentHtml, { searchable = false, below = false } = {}) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const m = el(`<div class="menu">
    ${searchable ? '<input class="menu-search" placeholder="Search…">' : ""}
    <div class="menu-list">${contentHtml}</div></div>`);
  document.body.appendChild(m);

  // Pinned to the trigger by the edge that faces it, never by a height measured
  // once: filtering a 400-model list shrinks the menu, and a `top` computed for
  // the tall version would leave it floating far above its pill.
  const place = () => {
    const r = anchor.getBoundingClientRect();
    const room = below ? window.innerHeight - r.bottom - GAP * 2 : r.top - GAP * 2;
    m.style.maxHeight = Math.max(160, Math.min(window.innerHeight * 0.6, room)) + "px";
    // Composer pills open upward; a top-bar anchor has no room above it.
    if (below) {
      m.style.top = r.bottom + GAP + "px";
      m.style.bottom = "auto";
    } else {
      m.style.bottom = window.innerHeight - r.top + GAP + "px";
      m.style.top = "auto";
    }
    m.style.left = Math.max(GAP, Math.min(r.left, window.innerWidth - m.offsetWidth - 12)) + "px";
  };
  place();
  // Freeze the width the full list asked for: without it every keystroke in
  // the search box resizes the card between the min and max width.
  m.style.width = m.offsetWidth + "px";

  // The handlers survive `m.remove()` calls made by the callers, so each one
  // unregisters itself the moment the menu is gone.
  const cleanup = () => {
    document.removeEventListener("mousedown", dismiss, true);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", onResize);
  };
  const gone = () => {
    if (m.isConnected) return false;
    cleanup();
    return true;
  };
  const close = () => { m.remove(); cleanup(); };
  const dismiss = (e) => { if (!gone() && !m.contains(e.target)) close(); };
  const onKey = (e) => {
    if (gone() || e.key !== "Escape") return;
    e.preventDefault();
    e.stopImmediatePropagation();   // closing a menu must not interrupt the agent
    close();
  };
  // Scrolling the menu's own list keeps the menu; anything else moved the
  // anchor, so follow it rather than leaving a detached card behind.
  const onScroll = (e) => {
    if (gone()) return;
    if (e.target === m || (e.target.nodeType === 1 && m.contains(e.target))) return;
    place();
  };
  const onResize = () => { if (!gone()) place(); };

  setTimeout(() => {
    if (gone()) return;
    document.addEventListener("mousedown", dismiss, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
  }, 0);
  // Callers close through this, so the listeners go with the node. (A stray
  // `m.remove()` elsewhere is still safe: every handler checks isConnected.)
  m.closeMenu = close;
  return m;
}

export const MODES = [
  ["plan", "Plan mode", "Read-only exploration; the agent submits a plan for your review before touching anything."],
  ["ask", "Ask mode", "Every mutating action (writes, edits, shell) asks for permission first."],
  ["auto-edit", "Auto-edit mode", "File edits inside the project run automatically; shell commands still ask."],
  ["dontask", "Don't-ask mode", "Never prompts — mutating actions outside the allow rules are denied."],
  ["yolo", "Yolo mode", "Skips all permission prompts (requires launching with --yolo)."],
];

export function openModeMenu(anchor) {
  const cur = store.state?.mode;
  const allowYolo = store.bootstrap?.allow_yolo;
  const items = MODES
    .filter(([id]) => id !== "yolo" || allowYolo)
    .map(([id, title, desc]) => `<button class="menu-item" data-mode="${id}">
      <div class="mi-title">${title}${cur === id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-desc">${desc}</div></button>`).join("");
  const m = menuAt(anchor, items);
  m.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (b) { actions.setMode(b.dataset.mode); m.closeMenu(); }
  });
}

export async function openModelMenu(anchor) {
  let models = [];
  try { models = await api.models(); } catch { /* offline */ }
  const cur = store.state?.model;
  // The whole catalog, not a slice of it: the search box is what makes 400
  // entries navigable, and a cap would hide the model somebody came for.
  const render = (list) => list.map((mo) => `
    <button class="menu-item" data-model="${esc(mo.id)}" title="${esc(mo.id)}">
      <div class="mi-title"><span class="mi-name">${esc(mo.name || mo.id)}</span>${
        cur === mo.id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-meta">${esc(mo.id)} · ctx ${fmtTokens(mo.context_length)}${
        mo.prompt_price != null ? ` · $${mo.prompt_price}/M in` : ""}</div>
    </button>`).join("") || `<div class="menu-note">No models match.</div>`;
  const m = menuAt(anchor, render(models), { searchable: true });
  const list = m.querySelector(".menu-list");
  const search = m.querySelector(".menu-search");
  // A pinned footer, so the escape hatch stays reachable without scrolling
  // past the whole catalog.
  const foot = el(`<div class="menu-foot">
    <button class="menu-item" data-custom>
      <div class="mi-title">Custom model id…</div>
      <div class="mi-desc">Use any id the provider accepts, listed or not.</div>
    </button></div>`);
  m.appendChild(foot);
  const customDesc = foot.querySelector(".mi-desc");
  search?.focus();
  search?.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    list.innerHTML = render(models.filter((mo) =>
      (mo.id + " " + (mo.name || "")).toLowerCase().includes(q)));
    list.scrollTop = 0;
    customDesc.textContent = q
      ? `Use “${search.value.trim()}” as the model id.`
      : "Use any id the provider accepts, listed or not.";
  });
  m.addEventListener("click", (e) => {
    if (e.target.closest("[data-custom]")) {
      const typed = search?.value.trim() || "";
      m.closeMenu();
      askCustomModel(typed);
      return;
    }
    const b = e.target.closest("[data-model]");
    if (b) { actions.setModel(b.dataset.model); m.closeMenu(); }
  });
}

/** Free-text model id. The backend takes any string; an id the catalog does
 *  not know simply comes back with no context length until the provider says. */
function askCustomModel(prefill = "") {
  const m = modal(
    "Custom model id",
    `<div style="font-size:13px;color:var(--fg-dim);margin-bottom:10px">
       The catalog is a convenience, not a gate — anything your provider accepts
       works here. An id it does not list keeps the context meter blank.</div>
     <input class="deny-input" id="custom-model" spellcheck="false"
            placeholder="e.g. vendor/model-name" value="${esc(prefill)}">`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary" data-use>Use this model</button>`,
  );
  const input = m.querySelector("#custom-model");
  input.focus();
  input.select();
  const use = () => {
    const v = input.value.trim();
    if (!v) { input.focus(); return; }
    actions.setModel(v);
    closeModal();
  };
  m.querySelector("[data-use]").addEventListener("click", use);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); use(); }
  });
}

// ---- help ----
//
// `?` stays a modal, and that is a decision rather than an oversight. It is
// pressed mid-sentence to remember one shortcut, and answering that with a view
// transition — losing the transcript you were looking at, changing the URL —
// would be a worse app for the commonest question. So the fast reference stays
// exactly where people expect it.
//
// What the modal is no longer allowed to be is the *whole* of the help. It now
// ends in a link into #/help, which is the surface that can explain how the
// pieces fit together, and it reads its two lists from js/help/shortcuts.js so
// the quick reference and the full one cannot drift apart.

export function openHelp({ onFull } = {}) {
  const row = ([k, d]) =>
    `<div class="help-row"><span class="help-key">${k}</span>
       <span class="help-desc">${d}</span></div>`;
  const m = modal("Help", `
    <div class="help-sec">
      <h4>Keyboard</h4>
      ${KEYS.map(([k, d]) => row([esc(k), esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Slash commands</h4>
      ${SLASH.map(([cmd, arg, d]) =>
        row([esc(cmd) + (arg ? " " + esc(arg) : ""), esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Permission modes</h4>
      ${MODES.map(([id, , desc]) => row([esc(id), esc(desc)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Side panel</h4>
      <div class="help-note">${esc(PANEL_NOTE)}</div>
    </div>`,
    `<button class="btn primary" id="help-full">Open the full help →</button>`);
  // Three places open this modal (the top bar, Home, and /help) and none of
  // them cares where "full help" goes, so the default is the route itself. The
  // callback exists for the one caller that wants to remember where it came
  // from; without it the link still works, which is what keeps this button from
  // ever being the dead control it would otherwise become.
  m.querySelector("#help-full").addEventListener("click", () => {
    closeModal();
    if (onFull) onFull();
    else location.hash = "#/help/overview";
  });
  return m;
}

// ---- multi-select ----
//
// One selection model, shared by the three lists that have one: the session
// rows on a Home card, the session rows in this file's switcher popover, and
// the project cards. It is deliberately dumb — a set of ids plus the anchor a
// shift-click extends from — and it never re-renders anything itself. Callers
// mutate it and then redraw, which is what lets a selection survive a re-render
// (the ids outlive the DOM) while a caller that drops the model on view change
// gets the other half of the rule for free.

/** A set of selected ids with shift-range extension. `order` is always the ids
 *  currently on screen, in display order — the model holds no DOM. */
export function makeSelection() {
  const ids = new Set();
  let anchor = null;
  return {
    get size() { return ids.size; },
    has: (id) => ids.has(id),
    clear() {
      const had = ids.size > 0;
      ids.clear();
      anchor = null;
      return had;   // so Escape can tell whether it consumed the keystroke
    },
    /** Toggle one row. With `shift`, every row between the anchor and this one
     *  takes this row's new state — the ordinary list-box behaviour. */
    toggle(id, order = [], shift = false) {
      const from = order.indexOf(anchor);
      const to = order.indexOf(id);
      if (shift && anchor !== null && from !== -1 && to !== -1 && from !== to) {
        const on = !ids.has(id);
        const [lo, hi] = from < to ? [from, to] : [to, from];
        for (const other of order.slice(lo, hi + 1)) {
          if (on) ids.add(other); else ids.delete(other);
        }
      } else {
        if (ids.has(id)) ids.delete(id); else ids.add(id);
      }
      anchor = id;
    },
    setAll(order, on) {
      for (const id of order) { if (on) ids.add(id); else ids.delete(id); }
      anchor = null;
    },
    /** Forget ids that are no longer on screen. Called after every reload, so a
     *  row deleted by somebody else cannot linger in a count. */
    keepOnly(order) {
      const live = new Set(order);
      for (const id of [...ids]) if (!live.has(id)) ids.delete(id);
    },
    inOrder(order) { return order.filter((id) => ids.has(id)); },
  };
}

const REASON_WORDS = {
  live: (n) => `${n} still open`,
  missing: (n) => `${n} already gone`,
  unknown: (n) => `${n} no longer listed`,
  failed: (n) => `${n} could not be deleted`,
};

/** Report a bulk result honestly: three of five is never "done".
 *
 *  `skipped` is the backend's per-row list, `[{reason, …}]`; both bulk routes
 *  answer in that shape. A partial result is deliberately not an error toast —
 *  something did happen — but it never reads as a plain success either. */
export function reportBulk(doneCount, skipped, { one, many, verb = "Deleted" }) {
  const count = (n) => `${n} ${n === 1 ? one : many}`;
  const groups = new Map();
  for (const s of skipped || []) {
    groups.set(s.reason, (groups.get(s.reason) || 0) + 1);
  }
  const why = [...groups].map(([reason, n]) =>
    (REASON_WORDS[reason] || ((k) => `${k} skipped`))(n)).join(", ");
  if (!why) {
    if (doneCount) toastOk(`${verb} ${count(doneCount)}.`);
    return;
  }
  if (!doneCount) {
    toastError(`Nothing was deleted — ${why}.`);
    return;
  }
  toast(`${verb} ${count(doneCount)}; ${skipped.length} left alone (${why}).`,
    { kind: "info", timeout: 7000 });
}

// ---- deleting QuickCode's data for a project ----

/** The one destructive action here that gets a real dialog rather than the
 *  arm-then-act button.
 *
 *  Removing a project from the list is reversible by reopening the folder, so
 *  a two-click button is proportionate. This is not: it unlinks transcripts,
 *  task boards and artifacts that exist nowhere else. So it names the exact
 *  directory it will remove, lists what is inside it, and says out loud the
 *  thing the user actually needs to know — that the code is not part of it.
 *
 *  `projects` is `[{id, name, path}]`; the same dialog covers a bulk purge. */
export function openPurgeProjects(projects, { onDone } = {}) {
  const many = projects.length > 1;
  const m = modal(
    many ? `Delete QuickCode data for ${projects.length} projects` : "Delete QuickCode data",
    `<div class="purge">
       <div class="purge-lead">This removes the <code>.quickcode</code> folder inside
         ${many ? "each project" : "the project"} and forgets
         ${many ? "their" : "its"} trust decision. It cannot be undone.</div>
       <div class="purge-list"><div class="hs-note">reading…</div></div>
       <div class="purge-safe">Your files are not touched. Nothing outside
         <code>.quickcode</code> is removed, and the project folder itself stays
         exactly where it is.</div>
       <div class="rn-err"></div>
     </div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn danger" data-purge disabled>Delete the data</button>`,
  );
  const list = m.querySelector(".purge-list");
  const err = m.querySelector(".rn-err");
  const go = m.querySelector("[data-purge]");

  const line = (p, d) => {
    const bits = [];
    if (d?.sessions) bits.push(`${d.sessions} session${d.sessions === 1 ? "" : "s"}`);
    if (d?.archived) bits.push(`${d.archived} archived`);
    if (d?.boards) bits.push(`${d.boards} task board${d.boards === 1 ? "" : "s"}`);
    if (d?.artifacts) bits.push(`${d.artifacts} artifact${d.artifacts === 1 ? "" : "s"}`);
    const what = !d
      ? "could not read this project's data directory"
      : d.exists
        ? (bits.length ? bits.join(" · ") : "settings and cached state only")
        : "nothing stored here yet";
    return `<div class="purge-item">
      <div class="purge-name">${esc(p.name || p.path)}</div>
      <div class="purge-path"><code>${esc(d?.path || p.path)}</code></div>
      <div class="purge-what">${esc(what)}</div>
    </div>`;
  };

  (async () => {
    const summaries = await Promise.all(projects.map(async (p) => {
      try { return await api.projectData(p.id); } catch { return null; }
    }));
    list.innerHTML = projects.map((p, i) => line(p, summaries[i])).join("");
    go.disabled = false;
  })();

  go.addEventListener("click", async () => {
    go.disabled = true;
    go.textContent = "Deleting…";
    err.textContent = "";
    let result;
    try {
      result = many
        ? await api.purgeProjects(projects.map((p) => p.id))
        : { removed: [await api.purgeProjectData(projects[0].id)], skipped: [] };
    } catch (e) {
      go.disabled = false;
      go.textContent = "Delete the data";
      err.textContent = e.message;
      return;
    }
    closeModal();
    reportBulk(result.removed.length, result.skipped,
      { one: "project", many: "projects", verb: "Deleted the QuickCode data of" });
    if (onDone) onDone(result);
  });
  return m;
}

// ---- sessions ----

/** Arm-then-act on one button: the first click relabels it, the second runs.
 *  A `confirm()` here would block the window while the agent is streaming. */
function armButton(btn, prompt, resting) {
  if (btn.dataset.armed === "1") return true;
  btn.dataset.armed = "1";
  btn.classList.add("armed");
  btn.textContent = prompt;
  setTimeout(() => {
    if (!btn.isConnected || btn.dataset.armed !== "1") return;
    delete btn.dataset.armed;
    btn.classList.remove("armed");
    btn.textContent = resting;
  }, 4000);
  return false;
}

/** Rename one session.
 *
 *  Shared by the two places a session is listed — the switcher popover and the
 *  Home view — which differ only in which project the write is addressed to,
 *  hence `save`. The dialog keeps its own failure under the field rather than
 *  raising a toast: the form is still on screen, so the message has a home, and
 *  the toast is for the success, which arrives after the form is gone.
 *
 *  A rename is broadcast on `window` because the session being renamed may be
 *  the one that is open, and the chip and the tab strip in the top bar have to
 *  stop showing the old name. */
export function openRenameSession({ convId, title = "", save, onDone }) {
  const m = modal(
    "Rename session",
    `<div style="font-size:13px;color:var(--fg-dim);margin-bottom:10px">
       A name of your own, instead of the first thing you typed. Clear the field
       to go back to that derived name.</div>
     <input class="deny-input" id="rename-title" spellcheck="false" maxlength="200"
            placeholder="e.g. flaky auth test" value="${esc(title)}">
     <div class="rn-err"></div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary" data-save>Save name</button>`,
  );
  const input = m.querySelector("#rename-title");
  const err = m.querySelector(".rn-err");
  const btn = m.querySelector("[data-save]");
  input.focus();
  input.select();

  const commit = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    err.textContent = "";
    let result;
    try {
      result = await save(input.value);
    } catch (e) {
      btn.disabled = false;
      err.textContent = e.message;
      input.focus();
      return;
    }
    const named = result?.title || input.value.trim();
    closeModal();
    toastOk(`Renamed to “${oneLine(named, 60)}”.`);
    window.dispatchEvent(new CustomEvent("qc:session-renamed", {
      detail: { convId, title: named },
    }));
    if (onDone) onDone(named);
  };

  btn.addEventListener("click", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
  });
  return m;
}

/** Session switcher hanging off the top bar: the sessions of the current
 *  project plus "new session", each row renamable, deletable and archivable in
 *  place. Resolves nothing — it calls back. */
export async function openSessionMenu(anchor, { onPick, onNew }) {
  let sessions = [];
  // The archive comes along on every fetch so the footer can say how much is
  // filed away before anyone asks to see it.
  try { sessions = await api.sessions(true); } catch { /* server gone */ }
  let revealed = false;
  const cur = store.convId;
  // Local to this popover: closing it is leaving the list, and a selection must
  // not outlive the list it was made in.
  const sel = makeSelection();
  let order = [];

  const m = menuAt(
    anchor,
    `<button class="menu-item" data-new><div class="mi-title">＋ New session</div>
       <div class="mi-desc">Start an empty conversation in this project.</div></button>
     <div class="menu-sep"></div><div class="menu-rows"></div>`,
    { below: true },
  );
  const rowsEl = m.querySelector(".menu-rows");
  const foot = el(`<div class="menu-foot menu-tools"></div>`);
  m.appendChild(foot);

  const row = (s) => `
    <div class="menu-row${s.archived ? " archived" : ""}${
      sel.has(s.conv_id) ? " picked" : ""}">
      <label class="mi-pick" title="Select this session (shift-click for a range)">
        <input type="checkbox" data-pick="${esc(s.conv_id)}"${
          sel.has(s.conv_id) ? " checked" : ""}></label>
      <button class="menu-item" data-conv="${esc(s.conv_id)}"
              title="${esc(oneLine(s.title, 200))}">
        <div class="mi-title"><span class="mi-name">${esc(oneLine(s.title, 90))}</span>${
          s.archived ? '<span class="mi-tag">archived</span>' : ""}${
          s.conv_id === cur ? '<span class="check">✓</span>' : ""}</div>
        <div class="mi-meta">${s.live ? "● live · " : ""}${esc(oneLine(s.model, 28))} ·
          ${s.message_count} msgs · ${relTime(s.mtime)}</div>
      </button>
      <button class="mi-act" data-rename="${esc(s.conv_id)}"
        title="Rename this session">✎</button>
      <button class="mi-act" data-arch="${esc(s.conv_id)}" data-on="${!s.archived}"
        title="${s.archived ? "Restore to the list" : "Archive: keep the file, hide the row"}"
        >${s.archived ? "⇧" : "⇩"}</button>
      <button class="mi-act mi-del" data-del="${esc(s.conv_id)}"
        title="Delete this session and its task board">✕</button>
    </div>`;

  function render() {
    const archivedCount = sessions.filter((s) => s.archived).length;
    const emptyIds = sessions
      .filter((s) => !s.archived && !s.live && !s.message_count)
      .map((s) => s.conv_id);
    const visible = sessions.filter((s) => revealed || !s.archived);
    // The rows on screen, in the order they are drawn: what a shift-click
    // ranges over and what "select all" means.
    order = visible.map((s) => s.conv_id);
    sel.keepOnly(order);
    rowsEl.innerHTML = visible.length
      ? visible.map(row).join("")
      : `<div class="menu-note">${archivedCount && !revealed
          ? "Nothing here but the archive." : "No saved sessions in this project yet."}</div>`;
    const n = sel.size;
    const allOn = order.length > 0 && n === order.length;
    foot.innerHTML = `
      ${n ? `<div class="menu-selbar">
         <span class="msb-count">${n} selected</span>
         <button class="menu-tool" data-all="${allOn ? "off" : "on"}">${
           allOn ? "select none" : `select all ${order.length}`}</button>
         <button class="menu-tool msb-del" data-bulk-del>delete ${n} session${
           n === 1 ? "" : "s"}</button>
         <span class="msb-hint">Esc clears</span>
       </div>` : ""}
      ${order.length && !n ? `<button class="menu-tool" data-all="on"
         >select all ${order.length}</button>` : ""}
      ${emptyIds.length ? `<button class="menu-tool" data-sweep
         >clean up ${emptyIds.length} empty</button>` : ""}
      ${archivedCount ? `<button class="menu-tool" data-toggle-arch
         aria-pressed="${revealed}">${revealed ? "hide" : "show"} archived
         (${archivedCount})</button>` : ""}
      <div class="menu-err"></div>`;
    // Nothing to select, nothing to sweep and nothing archived: no footer rule.
    foot.style.display = order.length || emptyIds.length || archivedCount ? "" : "none";
  }

  const fail = (err) => {
    foot.style.display = "";
    const box = foot.querySelector(".menu-err");
    if (box) box.textContent = err.message;
  };

  // Called after every write in here, so it is also where the rest of the UI
  // hears about one: the top bar's tab strip is drawn from the same list and
  // would otherwise keep offering a session that was just deleted.
  async function refresh() {
    try { sessions = await api.sessions(true); } catch (err) { fail(err); return; }
    render();
    window.dispatchEvent(new CustomEvent("qc:sessions-changed"));
  }

  render();

  // Escape clears the selection before it closes the popover. Registered now,
  // synchronously, so it runs ahead of menuAt's own Escape handler — which is
  // added on a timeout and would otherwise close the menu out from under a
  // keystroke the user meant as "never mind, deselect".
  const onSelEsc = (e) => {
    if (!m.isConnected) { document.removeEventListener("keydown", onSelEsc, true); return; }
    if (e.key !== "Escape" || !sel.size) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    sel.clear();
    render();
  };
  document.addEventListener("keydown", onSelEsc, true);

  m.addEventListener("click", async (e) => {
    if (e.target.closest("[data-new]")) { m.closeMenu(); onNew(); return; }

    const pick = e.target.closest("[data-pick]");
    if (pick) {
      // The checkbox's own state is thrown away and redrawn from the model:
      // a shift-click changes rows other than the one that was clicked.
      sel.toggle(pick.dataset.pick, order, e.shiftKey);
      render();
      return;
    }

    const all = e.target.closest("[data-all]");
    if (all) { sel.setAll(order, all.dataset.all === "on"); render(); return; }

    const bulk = e.target.closest("[data-bulk-del]");
    if (bulk) {
      const picked = sel.inOrder(order);
      const resting = `delete ${picked.length} session${picked.length === 1 ? "" : "s"}`;
      if (!armButton(bulk, `delete ${picked.length}?`, resting)) return;
      bulk.textContent = "…";
      let result;
      try {
        result = await api.removeSessions(picked);
      } catch (err) {
        // The whole request failed, so nothing was deleted: hand the button
        // back rather than leaving an ellipsis where a control used to be.
        delete bulk.dataset.armed;
        bulk.classList.remove("armed");
        bulk.textContent = resting;
        fail(err);
        return;
      }
      sel.clear();
      reportBulk(result.deleted.length, result.skipped,
        { one: "session", many: "sessions" });
      await refresh();
      return;
    }

    const toggle = e.target.closest("[data-toggle-arch]");
    if (toggle) { revealed = !revealed; render(); return; }

    const sweep = e.target.closest("[data-sweep]");
    if (sweep) {
      const n = sessions.filter((s) => !s.archived && !s.live && !s.message_count).length;
      if (!armButton(sweep, `delete ${n}?`, `clean up ${n} empty`)) return;
      sweep.textContent = "…";
      // Re-render only on success: it rewrites the footer, which is where the
      // failure would have been shown.
      try { await api.cleanupSessions(); } catch (err) { fail(err); return; }
      await refresh();
      return;
    }

    // Renaming opens a dialog, and a menu cannot stay under one, so the
    // popover closes. The name it was showing is refreshed from the top bar
    // instead — which is where the renamed session is, if it is the open one.
    const ren = e.target.closest("[data-rename]");
    if (ren) {
      const convId = ren.dataset.rename;
      const current = sessions.find((s) => s.conv_id === convId)?.title || "";
      m.closeMenu();
      openRenameSession({
        convId,
        title: current,
        save: (title) => api.renameSession(convId, title),
      });
      return;
    }

    const arch = e.target.closest("[data-arch]");
    if (arch) {
      const on = arch.dataset.on === "true";
      arch.textContent = "…";
      try {
        await api.archiveSession(arch.dataset.arch, on);
      } catch (err) {
        arch.textContent = on ? "⇩" : "⇧";
        fail(err);
        return;
      }
      await refresh();
      return;
    }

    const del = e.target.closest("[data-del]");
    if (del) {
      if (!armButton(del, "delete?", "✕")) return;
      del.textContent = "…";
      try {
        await api.removeSession(del.dataset.del);
      } catch (err) {
        delete del.dataset.armed;
        del.classList.remove("armed");
        del.textContent = "✕";
        fail(err);
        return;
      }
      await refresh();
      return;
    }

    const b = e.target.closest("[data-conv]");
    if (b) { m.closeMenu(); onPick(b.dataset.conv); }
  });
}

// ---- directory browser ----

/** Folder picker for "Open folder…". Directory-only by design — the backing
 *  endpoint never reports files. `onPick` receives an absolute path. */
export function openDirBrowser(onPick) {
  const m = modal(
    "Open folder",
    `<div class="dirb">
       <div class="dirb-bar">
         <button class="ghost-btn dirb-up" title="Parent directory">↑</button>
         <input class="dirb-path" spellcheck="false" placeholder="Paste or type a path…">
         <button class="btn dirb-go">Go</button>
       </div>
       <div class="dirb-list"></div>
       <div class="dirb-msg"></div>
     </div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary dirb-select">Open this folder</button>`,
  );
  const pathInput = m.querySelector(".dirb-path");
  const listEl = m.querySelector(".dirb-list");
  const msgEl = m.querySelector(".dirb-msg");
  const upBtn = m.querySelector(".dirb-up");
  let here = null;
  let parent = null;

  async function go(path) {
    msgEl.textContent = "";
    listEl.innerHTML = `<div class="dirb-empty">loading…</div>`;
    let data;
    try {
      data = await api.dir(path);
    } catch (err) {
      listEl.innerHTML = "";
      msgEl.textContent = err.message;
      return;
    }
    here = data.path;
    parent = data.parent;
    pathInput.value = here;
    upBtn.disabled = !parent;
    listEl.innerHTML = data.dirs.length
      ? data.dirs.map((d) => `
          <button class="dirb-row" data-path="${esc(d.path)}">
            <span class="dirb-icon">${d.is_git ? "⎇" : "▸"}</span>
            <span class="dirb-name">${esc(d.name)}</span>
            ${d.is_git ? '<span class="dirb-git">git</span>' : ""}
          </button>`).join("")
      : `<div class="dirb-empty">No sub-folders here.</div>`;
  }

  listEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-path]");
    if (b) go(b.dataset.path);
  });
  upBtn.addEventListener("click", () => { if (parent) go(parent); });
  m.querySelector(".dirb-go").addEventListener("click", () => go(pathInput.value.trim()));
  pathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(pathInput.value.trim()); }
  });
  m.querySelector(".dirb-select").addEventListener("click", async () => {
    const chosen = pathInput.value.trim() || here;
    if (!chosen) return;
    msgEl.textContent = "opening…";
    try {
      const project = await api.openProject(chosen);
      closeModal();
      onPick(project);
    } catch (err) {
      msgEl.textContent = err.message;
    }
  });

  go(null);
  return m;
}

// ---- quick settings ----

/** The three install-level things worth changing without leaving the chat.
 *  Everything else — plugins, prompt, agents, compositions — lives in the
 *  configuration view, and the footer link is how you get there. */
export function openQuickSettings({ onFull } = {}) {
  const m = modal(
    "Quick settings",
    `<div class="qs-note">Per install, applied to new sessions. Everything else
       — tools, the prompt, agents, compositions — is in the full configuration
       view.</div>
     <div class="set-field"><label>Provider endpoint (base URL)</label>
       <input id="qs-baseurl" spellcheck="false" placeholder="loading…" disabled></div>
     <div class="set-field"><label>API key <span id="qs-key-state"></span></label>
       <input id="qs-apikey" type="password" placeholder="sk-… (stored encrypted at rest)"></div>
     <div class="set-field"><label>Theme</label>
       <div class="qs-themes" id="qs-themes"></div></div>
     <span class="set-flash" id="qs-msg"></span>`,
    `<button class="btn" id="qs-full">Open full configuration →</button>
     <button class="btn primary" id="qs-save">Save</button>`,
  );

  const msg = m.querySelector("#qs-msg");
  const url = m.querySelector("#qs-baseurl");
  const flashMsg = (text, kind = "ok") => {
    msg.className = `set-flash ${kind}`;
    msg.textContent = text;
  };

  (async () => {
    let bs = store.bootstrap;
    if (!bs) {
      try { bs = await api.bootstrap(); store.bootstrap = bs; } catch { bs = {}; }
    }
    if (!m.isConnected) return;
    url.disabled = false;
    url.value = bs.base_url || "";
    url.placeholder = "https://…";
    m.querySelector("#qs-key-state").innerHTML = bs.has_api_key
      ? '<span class="ok-note">· saved</span>'
      : `<span class="warn-note">· not set (or $${esc(bs.api_key_env || "")})</span>`;
    const presets = bs.theme_presets || {};
    const current = bs.theme || {};
    m.querySelector("#qs-themes").innerHTML = Object.entries(presets).map(([name, colors]) => `
      <button class="qs-theme" data-theme="${esc(name)}"
              ${colors.background === current.background ? 'data-current="1"' : ""}
              title="${esc(name)}">
        ${["background", "panel", "primary", "accent"].map(
          (k) => `<i style="background:${esc(colors[k] || "#000")}"></i>`).join("")}
        <span>${esc(name)}</span>
      </button>`).join("");
    m.querySelector("#qs-themes").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-theme]");
      if (!b) return;
      const colors = presets[b.dataset.theme];
      applyTheme(colors);
      store.bootstrap = { ...(store.bootstrap || {}), theme: colors };
      m.querySelectorAll("[data-theme]").forEach((x) =>
        x.toggleAttribute("data-current", x === b));
      try {
        await api.putConfig({ theme: colors });
        flashMsg(`Theme “${b.dataset.theme}” saved.`);
      } catch (err) {
        flashMsg("Theme not saved: " + err.message, "err");
      }
    });
  })();

  m.querySelector("#qs-save").addEventListener("click", async () => {
    try {
      await api.putConfig({ base_url: url.value.trim() });
      const key = m.querySelector("#qs-apikey").value.trim();
      if (key) await api.putApiKey(key);
      store.bootstrap = { ...(store.bootstrap || {}), base_url: url.value.trim() };
      flashMsg("Saved. New sessions pick this up.");
    } catch (err) {
      flashMsg("Save failed: " + err.message, "err");
    }
  });
  m.querySelector("#qs-full").addEventListener("click", () => {
    closeModal();
    onFull?.();
  });
  return m;
}
