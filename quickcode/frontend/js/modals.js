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
}

function modal(title, bodyHtml, footHtml = "") {
  closeModal();
  const m = el(`<div class="modal-backdrop"><div class="modal" tabindex="-1"
       role="dialog" aria-modal="true" aria-label="${esc(String(title))}">
    <div class="modal-head"><span>${title}</span>
      <button class="ghost-btn" data-close>✕</button></div>
    <div class="modal-body">${bodyHtml}</div>
    ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
  </div></div>`);
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.closest("[data-close]")) closeModal();
  });
  root().appendChild(m);
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
  // Move focus into the dialog: Escape and Tab should belong to it from the
  // first keystroke, not to whatever button opened it.
  m.querySelector(".modal").focus();
  return m;
}

// ---- permission review ----

const shownReviews = new Set();

export function initReviews() {
  subscribe((kind, ev) => {
    if (kind === "event" && ev.type === "permission_request") maybeShowPermission(ev);
    if (kind === "event" && ev.type === "plan_request") maybeShowPlan(ev);
    if (kind === "state" && ev.pending) {
      for (const p of ev.pending) {
        if (p.kind === "permission") maybeShowPermission(p);
        if (p.kind === "plan") maybeShowPlan(p);
      }
    }
    if (kind === "event" && (ev.type === "permission_resolved" || ev.type === "plan_resolved")) {
      if (shownReviews.has(ev.req_id)) { shownReviews.delete(ev.req_id); closeModal(); }
    }
  });
}

function maybeShowPermission(ev) {
  if (shownReviews.has(ev.req_id)) return;
  // During replay only surface requests the server still reports as pending.
  if (store.replaying && !(store.state?.pending || []).some((p) => p.req_id === ev.req_id)) return;
  shownReviews.add(ev.req_id);
  const m = modal(
    "Permission required",
    `<div>The agent wants to run
       <span class="perm-tool">${esc(ev.tool)}</span>
       ${ev.agent && ev.agent !== "main" ? `(subagent ${esc(ev.agent)})` : ""}</div>
     <div class="perm-preview">${esc(ev.preview || ev.arg)}</div>
     <div style="font-size:12px;color:var(--fg-dim)">Always-allow saves the rule
       <code>${esc(ev.rule_suggestion)}</code> to .quickcode/settings.local.json</div>
     <input class="deny-input hidden" placeholder="Why not? (optional — steers the agent)">`,
    `<button class="btn danger" data-act="deny">Deny</button>
     <button class="btn" data-act="always">Always allow</button>
     <button class="btn primary" data-act="allow">Allow once</button>`
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
    closeModal();
    shownReviews.delete(ev.req_id);
  });
}

function maybeShowPlan(ev) {
  if (shownReviews.has(ev.req_id)) return;
  if (store.replaying && !(store.state?.pending || []).some((p) => p.req_id === ev.req_id)) return;
  shownReviews.add(ev.req_id);
  const m = modal(
    "Plan review",
    `<div class="perm-preview" style="max-height:52vh">${esc(ev.plan)}</div>
     <input class="deny-input hidden" placeholder="Feedback for the next iteration…">`,
    `<button class="btn" data-act="revise">Keep planning</button>
     <button class="btn" data-act="approve-ask">Approve · ask mode</button>
     <button class="btn primary" data-act="approve-auto">Approve · auto-edit</button>`
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
    closeModal();
    shownReviews.delete(ev.req_id);
  });
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

/** Session switcher hanging off the top bar: the sessions of the current
 *  project plus "new session", each row deletable and archivable in place.
 *  Resolves nothing — it calls back. */
export async function openSessionMenu(anchor, { onPick, onNew }) {
  let sessions = [];
  // The archive comes along on every fetch so the footer can say how much is
  // filed away before anyone asks to see it.
  try { sessions = await api.sessions(true); } catch { /* server gone */ }
  let revealed = false;
  const cur = store.convId;

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
    <div class="menu-row${s.archived ? " archived" : ""}">
      <button class="menu-item" data-conv="${esc(s.conv_id)}"
              title="${esc(oneLine(s.title, 200))}">
        <div class="mi-title"><span class="mi-name">${esc(oneLine(s.title, 90))}</span>${
          s.archived ? '<span class="mi-tag">archived</span>' : ""}${
          s.conv_id === cur ? '<span class="check">✓</span>' : ""}</div>
        <div class="mi-meta">${s.live ? "● live · " : ""}${esc(oneLine(s.model, 28))} ·
          ${s.message_count} msgs · ${relTime(s.mtime)}</div>
      </button>
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
    rowsEl.innerHTML = visible.length
      ? visible.map(row).join("")
      : `<div class="menu-note">${archivedCount && !revealed
          ? "Nothing here but the archive." : "No saved sessions in this project yet."}</div>`;
    foot.innerHTML = `
      ${emptyIds.length ? `<button class="menu-tool" data-sweep
         >clean up ${emptyIds.length} empty</button>` : ""}
      ${archivedCount ? `<button class="menu-tool" data-toggle-arch
         aria-pressed="${revealed}">${revealed ? "hide" : "show"} archived
         (${archivedCount})</button>` : ""}
      <div class="menu-err"></div>`;
    // Nothing to sweep and nothing archived: no footer rule, no empty strip.
    foot.style.display = emptyIds.length || archivedCount ? "" : "none";
  }

  const fail = (err) => {
    foot.style.display = "";
    const box = foot.querySelector(".menu-err");
    if (box) box.textContent = err.message;
  };

  async function refresh() {
    try { sessions = await api.sessions(true); } catch (err) { fail(err); return; }
    render();
  }

  render();

  m.addEventListener("click", async (e) => {
    if (e.target.closest("[data-new]")) { m.closeMenu(); onNew(); return; }

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
