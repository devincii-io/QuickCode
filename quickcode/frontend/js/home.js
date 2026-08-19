// Home view: the landing page when no project is selected. Recent projects as
// cards, each expandable into its session list, plus a directory browser for
// opening something new.
//
// The project registry (GET /api/projects) lists directories QuickCode has
// seen; a manager only exists for the ones currently open. Anything that needs
// a manager — the session list, opening a workspace — therefore posts to
// /api/projects/open first, which is idempotent and returns the same id.

import { api } from "./api.js";
import {
  makeSelection,
  openDirBrowser,
  openPurgeProjects,
  openRenameSession,
  reportBulk,
} from "./modals.js";
import { toastError } from "./toast.js";
import { armed, trustSummary } from "./trust.js";
import { el, esc, oneLine, relTime, wireLogo } from "./util.js";

const LAST_PROJECT_KEY = "qc-last-project";

let root, listEl;
let onOpen = () => {};
const expanded = new Set();   // project ids whose session list is open
const showArchived = new Set(); // project ids currently revealing their archive

// ---- selections ----
//
// Two levels, because there are two lists: the project cards, and the session
// rows inside one card. Both live here rather than in the DOM, which is what
// makes a selection survive a re-render — expanding a card, toggling its
// archive, deleting one row — while `refreshHome()` drops both, so leaving Home
// and coming back never resurrects a selection made against a different screen.

const projectSel = makeSelection();
const sessionSel = new Map();   // pid -> selection over that project's sessions

function sessionsOf(pid) {
  if (!sessionSel.has(pid)) sessionSel.set(pid, makeSelection());
  return sessionSel.get(pid);
}

function clearSelections() {
  projectSel.clear();
  sessionSel.clear();
}

/** Escape clears whatever is selected on Home, innermost first: a session
 *  selection inside a card before the project selection around it. Returns true
 *  when it consumed the keystroke. */
function clearOne() {
  for (const [pid, sel] of sessionSel) {
    if (sel.clear()) {
      const card = listEl?.querySelector(`.home-card[data-pid="${CSS.escape(pid)}"]`);
      const slot = card?.querySelector(".hs-list");
      if (slot?._rerender) slot._rerender();
      return true;
    }
  }
  if (projectSel.clear()) { syncProjectPicks(); return true; }
  return false;
}

export function lastProject() {
  try { return localStorage.getItem(LAST_PROJECT_KEY) || null; } catch { return null; }
}

export function rememberProject(pid) {
  try { localStorage.setItem(LAST_PROJECT_KEY, pid); } catch { /* quota */ }
}

// ---- formatting ----

// last_opened is an ISO timestamp; relTime speaks epoch seconds.
function relIso(iso) {
  if (!iso) return "never opened";
  const t = Date.parse(iso);
  return isNaN(t) ? "never opened" : relTime(t / 1000);
}

// Keep the tail: the last two segments say more than the drive letter does.
function shortPath(path) {
  const parts = String(path).replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 3) return path;
  return "…/" + parts.slice(-3).join("/");
}

// ---- rendering ----

function cardNode(p, isLast) {
  const card = el(`<article class="home-card${
    projectSel.has(p.id) ? " picked" : ""}" data-pid="${esc(p.id)}">
    <label class="hc-pick" title="Select this project (shift-click for a range)">
      <input type="checkbox" class="hc-check"${projectSel.has(p.id) ? " checked" : ""}
             aria-label="Select ${esc(p.name || p.path)}"></label>
    <button class="hc-main">
      <div class="hc-top">
        <span class="hc-name">${esc(p.name || p.path)}</span>
        ${p.live_sessions > 0 ? '<span class="hc-live">● live</span>' : ""}
        ${isLast ? '<span class="hc-last">last</span>' : ""}
      </div>
      <div class="hc-path" title="${esc(p.path)}">${esc(shortPath(p.path))}</div>
      <div class="hc-meta">
        <span>${esc(relIso(p.last_opened))}</span>
        <span class="hc-dot">·</span>
        <span>${p.session_count} session${p.session_count === 1 ? "" : "s"}</span>
      </div>
    </button>
    <div class="hc-actions">
      <button class="hc-new" title="Start a new conversation in this project">＋ New chat</button>
      <button class="hc-expand" title="Show sessions"
              aria-expanded="false">sessions ▾</button>
    </div>
    <div class="hc-sessions"></div>
  </article>`);

  // Bound to the box, not to the label around it: a label click is forwarded to
  // its input and would otherwise be counted twice.
  card.querySelector(".hc-check").addEventListener("click", (e) => {
    // The browser has already flipped the box; the model is the authority, so
    // sync() writes it back — a shift-click changes cards other than this one.
    e.stopPropagation();
    projectSel.toggle(p.id, projectOrder(), e.shiftKey);
    syncProjectPicks();
  });
  card.querySelector(".hc-main").addEventListener("click", () => open(p));
  card.querySelector(".hc-new").addEventListener("click", () => open(p));
  const expandBtn = card.querySelector(".hc-expand");
  const sessionsEl = card.querySelector(".hc-sessions");
  expandBtn.addEventListener("click", () => {
    const nowOpen = !card.classList.contains("open");
    card.classList.toggle("open", nowOpen);
    expandBtn.setAttribute("aria-expanded", nowOpen ? "true" : "false");
    expandBtn.textContent = nowOpen ? "sessions ▴" : "sessions ▾";
    if (nowOpen) { expanded.add(p.id); loadSessions(p, sessionsEl); }
    else { expanded.delete(p.id); sessionsEl.innerHTML = ""; }
  });
  if (expanded.has(p.id)) {
    card.classList.add("open");
    expandBtn.setAttribute("aria-expanded", "true");
    expandBtn.textContent = "sessions ▴";
    loadSessions(p, sessionsEl);
  }
  return card;
}

// The arm-then-act helper this file used to define now lives in trust.js, so
// the trust prompt and the destructive actions here share one implementation.

/** Hand an armed button back after a failure: the request did not happen, so
 *  the next click must be the first one again, not the confirmation. */
function disarm(btn, resting) {
  delete btn.dataset.armed;
  btn.classList.remove("armed");
  btn.textContent = resting;
}

// ---- the project selection bar ----

/** The project ids on screen, in the order they are drawn — what a shift-click
 *  ranges over and what "select all" means. Read from the DOM because that is
 *  the only place that knows what is currently rendered. */
function projectOrder() {
  return [...listEl.querySelectorAll(".home-card")].map((c) => c.dataset.pid);
}

function projectInfo(pid) {
  const card = listEl.querySelector(`.home-card[data-pid="${CSS.escape(pid)}"]`);
  return {
    id: pid,
    name: card?.querySelector(".hc-name")?.textContent || pid,
    path: card?.querySelector(".hc-path")?.title || "",
  };
}

/** Redraw the checkboxes and the bar from the model, without rebuilding the
 *  cards: rebuilding would collapse every expanded session list. */
function syncProjectPicks() {
  const order = projectOrder();
  projectSel.keepOnly(order);
  for (const card of listEl.querySelectorAll(".home-card")) {
    const on = projectSel.has(card.dataset.pid);
    card.classList.toggle("picked", on);
    const box = card.querySelector(".hc-check");
    if (box) box.checked = on;
  }
  renderProjectBar(order);
}

function renderProjectBar(order) {
  const existing = listEl.querySelector(".home-selbar");
  const n = projectSel.size;
  // The bar exists only while something is selected; nothing is selected on
  // arrival, so Home opens looking exactly as it always has.
  if (!n) { existing?.remove(); return; }
  const allOn = order.length > 0 && n === order.length;
  const bar = el(`<div class="home-selbar">
    <span class="hsb-count">${n} selected</span>
    <button class="hs-tool hsb-all">${allOn ? "select none" : `select all ${order.length}`}</button>
    <span class="hsb-spacer"></span>
    <button class="hs-tool hsb-remove">Remove ${n} project${
      n === 1 ? "" : "s"} from the list</button>
    <button class="hs-tool hsb-purge">Delete QuickCode data…</button>
    <span class="hsb-hint">Esc clears</span>
  </div>`);
  if (existing) existing.replaceWith(bar); else listEl.prepend(bar);

  bar.querySelector(".hsb-all").addEventListener("click", () => {
    projectSel.setAll(order, !allOn);
    syncProjectPicks();
  });

  const remove = bar.querySelector(".hsb-remove");
  const resting = `Remove ${n} project${n === 1 ? "" : "s"} from the list`;
  remove.addEventListener("click", async () => {
    // Reversible — reopening the folder brings the entry back, unchanged — so
    // the two-click button is proportionate and no dialog is warranted.
    if (!armed(remove, `remove ${n}?`, resting)) return;
    remove.textContent = "…";
    const picked = projectSel.inOrder(order);
    let result;
    try {
      result = await api.removeProjects(picked);
    } catch (err) {
      disarm(remove, resting);
      toastError(err.message);
      return;
    }
    projectSel.clear();
    reportBulk(result.removed.length, result.skipped,
      { one: "project", many: "projects", verb: "Removed" });
    refreshHome();
  });

  bar.querySelector(".hsb-purge").addEventListener("click", () => {
    // The destructive one gets a real dialog: it names the exact directory and
    // lists what is inside it before anything is unlinked.
    openPurgeProjects(projectSel.inOrder(order).map(projectInfo), {
      onDone: () => { projectSel.clear(); refreshHome(); },
    });
  });
}

/** A card that is open says whether the project's own MCP servers are running.
 *  Nothing is rendered for a project that declares none, which is most of them;
 *  the decision itself is taken in the workspace, where the commands are on
 *  screen, so this row only reports and offers the revoke. */
async function trustRow(p, container, reload) {
  const status = await trustSummary(p.id);
  if (!status?.has_servers) return;
  const n = status.servers.length;
  const word = n === 1 ? "server" : "servers";
  const row = status.inert
    ? el(`<div class="hs-trust warn">
        <span class="ht-text">△ ${n} MCP ${word} declared here ${
          n === 1 ? "is" : "are"} not running until you trust this project.</span>
        <button class="hs-tool ht-review">review</button>
      </div>`)
    : el(`<div class="hs-trust ok">
        <span class="ht-text">✓ Trusted to run ${n} MCP ${word}: ${
          esc(status.servers.join(", "))}.</span>
        <button class="hs-tool ht-revoke">revoke</button>
      </div>`);

  row.querySelector(".ht-review")?.addEventListener("click", () => open(p));
  const revoke = row.querySelector(".ht-revoke");
  revoke?.addEventListener("click", async () => {
    if (!armed(revoke, "revoke?", "revoke")) return;
    revoke.textContent = "…";
    try {
      await api.revokeTrustOf(p.id);
      reload();
    } catch (err) {
      delete revoke.dataset.armed;
      revoke.classList.remove("armed");
      revoke.textContent = "revoke";
      revoke.title = err.message;
    }
  });
  container.replaceChildren(row);
}

async function loadSessions(p, container) {
  container.innerHTML = `<div class="hs-note">loading…</div>`;
  let sessions;
  try {
    // The registry knows the path; only an open project has sessions to list.
    await api.openProject(p.path);
    // Always fetch the archive too: the toolbar has to be able to say how
    // many sessions are filed away before the user asks to see them.
    sessions = await api.sessionsOf(p.id, true);
  } catch (err) {
    container.innerHTML = `<div class="hs-note hs-err">${esc(err.message)}</div>`;
    return;
  }
  // Two slots: the trust report keeps its own, so re-rendering the list (the
  // archive toggle does that) never wipes it.
  container.innerHTML = "";
  const trustSlot = el(`<div class="hs-trust-slot"></div>`);
  const listSlot = el(`<div class="hs-list"></div>`);
  container.append(trustSlot, listSlot);
  renderSessions(p, listSlot, sessions);
  // After the rows, so a slow trust report never delays the session list.
  trustRow(p, trustSlot, () => loadSessions(p, container));
}

function renderSessions(p, container, sessions) {
  const revealed = showArchived.has(p.id);
  const archivedCount = sessions.filter((s) => s.archived).length;
  // Abandoned launches: no messages and no transcript events. The backend is
  // the authority on that; message_count already folds in the event-only case,
  // so a session interrupted mid-turn never shows up here.
  const emptyCount = sessions.filter((s) => !s.archived && !s.live && !s.message_count).length;
  const visible = sessions.filter((s) => revealed || !s.archived);
  const sel = sessionsOf(p.id);
  // The rows on screen, in draw order: the range a shift-click spans, and the
  // only ids a selection is allowed to name.
  const order = visible.map((s) => s.conv_id);
  sel.keepOnly(order);
  const redraw = () => renderSessions(p, container, sessions);
  // Escape reaches this list through the slot, which is the node that outlives
  // each individual render.
  container._rerender = redraw;
  // `container` is the list slot; loadSessions owns the whole `.hc-sessions`
  // box (trust report and list), which is its parent.
  const reload = () => loadSessions(p, container.parentElement || container);

  container.innerHTML = "";
  if (archivedCount || emptyCount || order.length) {
    const bar = el(`<div class="hs-bar">
      ${order.length ? `<button class="hs-tool hs-pick-all">${
        sel.size === order.length ? "select none" : `select all ${order.length}`}</button>` : ""}
      ${emptyCount ? `<button class="hs-tool hs-sweep">clean up ${emptyCount} empty</button>` : ""}
      ${archivedCount ? `<button class="hs-tool hs-arch-toggle"
          aria-pressed="${revealed}">${revealed ? "hide" : "show"} archived
          (${archivedCount})</button>` : ""}
    </div>`);
    bar.querySelector(".hs-pick-all")?.addEventListener("click", () => {
      sel.setAll(order, sel.size !== order.length);
      redraw();
    });
    bar.querySelector(".hs-arch-toggle")?.addEventListener("click", () => {
      if (revealed) showArchived.delete(p.id); else showArchived.add(p.id);
      // Deliberately kept: the archive toggle is a re-render of the same list,
      // and keepOnly() above drops whatever it hides.
      redraw();
    });
    const sweep = bar.querySelector(".hs-sweep");
    sweep?.addEventListener("click", async () => {
      if (!armed(sweep, `delete ${emptyCount}?`, `clean up ${emptyCount} empty`)) return;
      sweep.textContent = "…";
      try {
        await api.cleanupSessionsOf(p.id);
        reload();
      } catch (err) {
        sweep.textContent = "failed";
        sweep.title = err.message;
      }
    });
    container.appendChild(bar);
  }
  if (sel.size) container.appendChild(sessionSelBar(p, sel, order, reload));
  if (!visible.length) {
    container.appendChild(el(`<div class="hs-note">${archivedCount && !revealed
      ? "Nothing here but the archive."
      : "No saved sessions yet."}</div>`));
    return;
  }
  for (const s of visible) container.appendChild(sessionRow(p, s, reload, sel, order, redraw));
}

/** Shown only while rows are selected, and it says exactly what will happen. */
function sessionSelBar(p, sel, order, reload) {
  const n = sel.size;
  const resting = `Delete ${n} session${n === 1 ? "" : "s"}`;
  const bar = el(`<div class="hs-selbar">
    <span class="hsb-count">${n} selected</span>
    <span class="hsb-spacer"></span>
    <button class="hs-tool hsb-del">${resting}</button>
    <span class="hsb-hint">Esc clears</span>
  </div>`);
  const del = bar.querySelector(".hsb-del");
  del.addEventListener("click", async () => {
    if (!armed(del, `delete ${n}?`, resting)) return;
    del.textContent = "…";
    let result;
    try {
      result = await api.removeSessionsOf(p.id, sel.inOrder(order));
    } catch (err) {
      disarm(del, resting);
      toastError(err.message);
      return;
    }
    // Whatever survived (a live session, say) stays selected only if it is
    // still on screen; keepOnly() on the next render settles that.
    sel.clear();
    reportBulk(result.deleted.length, result.skipped, { one: "session", many: "sessions" });
    reload();
  });
  return bar;
}

function sessionRow(p, s, reload, sel, order, redraw) {
  const row = el(`<div class="hs-row${s.archived ? " archived" : ""}${
    sel.has(s.conv_id) ? " picked" : ""}">
    <label class="hs-pick" title="Select this session (shift-click for a range)">
      <input type="checkbox" class="hs-check"${sel.has(s.conv_id) ? " checked" : ""}
             aria-label="Select session ${esc(oneLine(s.title, 60))}"></label>
    <button class="hs-open">
      <span class="hs-title">${esc(oneLine(s.title, 70))}</span>
      ${s.live ? '<span class="hs-live">●</span>' : ""}
      ${s.archived ? '<span class="hs-tag">archived</span>' : ""}
      <span class="hs-meta">${esc(oneLine(s.model, 26))} · ${s.message_count} msgs ·
        ${esc(relTime(s.mtime))}</span>
    </button>
    <button class="hs-ren" title="Rename this session">✎</button>
    <button class="hs-arch" title="${s.archived
      ? "Restore this session to the list" : "Archive: keep the file, hide the row"}">${
      s.archived ? "⇧" : "⇩"}</button>
    <button class="hs-del" title="Delete this session and its task board">✕</button>
  </div>`);

  // Bound to the box itself: a click on the label around it is forwarded to the
  // input, and a handler on the label would count that twice.
  row.querySelector(".hs-check").addEventListener("click", (e) => {
    e.stopPropagation();
    sel.toggle(s.conv_id, order, e.shiftKey);
    redraw();
  });

  // Opening an archived session restores it — working in something is the
  // opposite of having filed it away (the backend does this too, on attach).
  row.querySelector(".hs-open").addEventListener("click", () => open(p, s.conv_id));

  const fail = (err) => {
    row.classList.add("hs-failed");
    const note = row.querySelector(".hs-error") || el(`<div class="hs-error"></div>`);
    note.textContent = err.message;
    row.appendChild(note);
  };

  // Renaming is not destructive and does not move the file, so it is the one
  // row action that is also offered for a session that is live somewhere.
  row.querySelector(".hs-ren").addEventListener("click", () => openRenameSession({
    convId: s.conv_id,
    title: s.title,
    save: (title) => api.renameSessionOf(p.id, s.conv_id, title),
    onDone: reload,
  }));

  const arch = row.querySelector(".hs-arch");
  arch.addEventListener("click", async () => {
    arch.textContent = "…";
    try {
      await api.archiveSessionOf(p.id, s.conv_id, !s.archived);
      reload();
    } catch (err) {
      arch.textContent = s.archived ? "⇧" : "⇩";
      fail(err);
    }
  });

  const del = row.querySelector(".hs-del");
  del.addEventListener("click", async () => {
    // Two-step, in place: the first click arms, the second deletes.
    if (!armed(del, "delete?", "✕")) return;
    del.textContent = "…";
    try {
      await api.deleteSession(p.id, s.conv_id);
      reload();
    } catch (err) {
      delete del.dataset.armed;
      del.classList.remove("armed");
      del.textContent = "✕";
      fail(err);
    }
  });
  return row;
}

async function open(p, resume) {
  let project = { id: p.id, path: p.path, name: p.name };
  try {
    // Guarantees the manager exists before the workspace asks it anything.
    project = await api.openProject(p.path);
  } catch (err) {
    listEl.prepend(el(`<div class="home-err">${esc(err.message)}</div>`));
    return;
  }
  rememberProject(project.id);
  // Leaving for a workspace ends the selection, here as well as in refreshHome:
  // whatever was ticked belonged to the screen being left.
  clearSelections();
  onOpen(project, { resume: resume || null });
}

// ---- public ----

export function initHome(hooks) {
  onOpen = hooks?.onOpen || (() => {});
  root = document.getElementById("view-home");
  listEl = document.getElementById("home-projects");

  wireLogo(root.querySelector(".home-logo img"), "home-logo-fallback");

  document.getElementById("home-open").addEventListener("click", () =>
    openDirBrowser((project) => {
      rememberProject(project.id);
      onOpen(project, { resume: null });
    }));
  document.getElementById("home-refresh").addEventListener("click", () => refreshHome());

  // Escape clears the selection, and only that: it is registered on the
  // document because the focus after a shift-click is on a checkbox, not on the
  // list. It stands down whenever Home is not the visible view, or a dialog or
  // popover is on top — those own Escape while they are up.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!document.getElementById("app").classList.contains("showing-home")) return;
    if (document.querySelector(".menu") || document.querySelector(".modal-backdrop")) return;
    if (clearOne()) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
}

export async function refreshHome() {
  // Reaching Home is arriving at a different screen, so no selection survives
  // it — including the one that was made on a project that is no longer here.
  clearSelections();
  listEl.innerHTML = `<div class="home-note">loading projects…</div>`;
  let data;
  try {
    data = await api.projects();
  } catch (err) {
    listEl.innerHTML = `<div class="home-err">Cannot reach the QuickCode backend
      (${esc(err.message)}). Open QuickCode through the URL printed by the CLI —
      it carries the auth token.</div>`;
    return;
  }
  const projects = data.projects || [];
  if (!projects.length) {
    listEl.innerHTML = `<div class="home-note">No projects yet. Open a folder to start.</div>`;
    return;
  }
  const last = lastProject();
  listEl.innerHTML = "";
  for (const p of projects) listEl.appendChild(cardNode(p, p.id === last));
}
