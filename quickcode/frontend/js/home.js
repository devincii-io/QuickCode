// Home view: the landing page when no project is selected. Recent projects as
// cards, each expandable into its session list, plus a directory browser for
// opening something new.
//
// The project registry (GET /api/projects) lists directories QuickCode has
// seen; a manager only exists for the ones currently open. Anything that needs
// a manager — the session list, opening a workspace — therefore posts to
// /api/projects/open first, which is idempotent and returns the same id.

import { api } from "./api.js";
import { openDirBrowser } from "./modals.js";
import { el, esc, oneLine, relTime, wireLogo } from "./util.js";

const LAST_PROJECT_KEY = "qc-last-project";

let root, listEl;
let onOpen = () => {};
const expanded = new Set();   // project ids whose session list is open
const showArchived = new Set(); // project ids currently revealing their archive

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
  const card = el(`<article class="home-card" data-pid="${esc(p.id)}">
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

/** Arm-then-act on the same button. Returns true once the caller may proceed;
 *  the first call only changes the label and disarms itself after a moment. */
function armed(btn, prompt, resting) {
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
  renderSessions(p, container, sessions);
}

function renderSessions(p, container, sessions) {
  const revealed = showArchived.has(p.id);
  const archivedCount = sessions.filter((s) => s.archived).length;
  // Abandoned launches: no messages and no transcript events. The backend is
  // the authority on that; message_count already folds in the event-only case,
  // so a session interrupted mid-turn never shows up here.
  const emptyCount = sessions.filter((s) => !s.archived && !s.live && !s.message_count).length;
  const visible = sessions.filter((s) => revealed || !s.archived);

  container.innerHTML = "";
  if (archivedCount || emptyCount) {
    const bar = el(`<div class="hs-bar">
      ${emptyCount ? `<button class="hs-tool hs-sweep">clean up ${emptyCount} empty</button>` : ""}
      ${archivedCount ? `<button class="hs-tool hs-arch-toggle"
          aria-pressed="${revealed}">${revealed ? "hide" : "show"} archived
          (${archivedCount})</button>` : ""}
    </div>`);
    bar.querySelector(".hs-arch-toggle")?.addEventListener("click", () => {
      if (revealed) showArchived.delete(p.id); else showArchived.add(p.id);
      renderSessions(p, container, sessions);
    });
    const sweep = bar.querySelector(".hs-sweep");
    sweep?.addEventListener("click", async () => {
      if (!armed(sweep, `delete ${emptyCount}?`, `clean up ${emptyCount} empty`)) return;
      sweep.textContent = "…";
      try {
        await api.cleanupSessionsOf(p.id);
        loadSessions(p, container);
      } catch (err) {
        sweep.textContent = "failed";
        sweep.title = err.message;
      }
    });
    container.appendChild(bar);
  }
  if (!visible.length) {
    container.appendChild(el(`<div class="hs-note">${archivedCount && !revealed
      ? "Nothing here but the archive."
      : "No saved sessions yet."}</div>`));
    return;
  }
  const reload = () => loadSessions(p, container);
  for (const s of visible) container.appendChild(sessionRow(p, s, reload));
}

function sessionRow(p, s, reload) {
  const row = el(`<div class="hs-row${s.archived ? " archived" : ""}">
    <button class="hs-open">
      <span class="hs-title">${esc(oneLine(s.title, 70))}</span>
      ${s.live ? '<span class="hs-live">●</span>' : ""}
      ${s.archived ? '<span class="hs-tag">archived</span>' : ""}
      <span class="hs-meta">${esc(oneLine(s.model, 26))} · ${s.message_count} msgs ·
        ${esc(relTime(s.mtime))}</span>
    </button>
    <button class="hs-arch" title="${s.archived
      ? "Restore this session to the list" : "Archive: keep the file, hide the row"}">${
      s.archived ? "⇧" : "⇩"}</button>
    <button class="hs-del" title="Delete this session and its task board">✕</button>
  </div>`);

  // Opening an archived session restores it — working in something is the
  // opposite of having filed it away (the backend does this too, on attach).
  row.querySelector(".hs-open").addEventListener("click", () => open(p, s.conv_id));

  const fail = (err) => {
    row.classList.add("hs-failed");
    const note = row.querySelector(".hs-error") || el(`<div class="hs-error"></div>`);
    note.textContent = err.message;
    row.appendChild(note);
  };

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
}

export async function refreshHome() {
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
