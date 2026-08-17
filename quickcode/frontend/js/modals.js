// Modals and dropdown menus: permission review, plan review, mode menu,
// model picker, sessions, settings (General / Models / Plugins).

import { api } from "./api.js";
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
    if (document.querySelector(".menu")) return;   // a menu on top closes first
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

const MODES = [
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

const HELP_KEYS = [
  ["Enter", "Send the message"],
  ["Shift + Enter", "Newline inside the composer"],
  ["Esc", "Interrupt the agent (when no menu or dialog is open)"],
  ["↑ / ↓", "Walk back and forward through sent-message history"],
  ["/", "Open the slash-command menu; Tab completes, Enter runs, Esc closes"],
];

const HELP_COMMANDS = [
  ["/compact", "Compress the conversation into a summary"],
  ["/clear", "Start a new conversation"],
  ["/mode &lt;plan|ask|auto-edit|dontask|yolo&gt;", "Switch the permission mode"],
  ["/model", "Pick the model for this session"],
  ["/help", "This reference"],
];

export function openHelp() {
  const row = ([k, d]) =>
    `<div class="help-row"><span class="help-key">${k}</span>
       <span class="help-desc">${d}</span></div>`;
  modal("Help", `
    <div class="help-sec">
      <h4>Keyboard</h4>
      ${HELP_KEYS.map(([k, d]) => row([esc(k), esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Slash commands</h4>
      ${HELP_COMMANDS.map(([k, d]) => row([k, esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Permission modes</h4>
      ${MODES.map(([id, , desc]) => row([esc(id), esc(desc)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Side panel</h4>
      <div class="help-note">The right-hand panel holds Trajectory, Agents,
        Tasks, Files and Usage. Drag its left edge to resize, press ⛶ to give
        it the whole window (Esc brings the chat back), and use the ⌕ trace
        links in the transcript to jump straight to an event.</div>
    </div>`);
}

// ---- sessions ----

/** Session switcher hanging off the top bar: the sessions of the current
 *  project plus "new session". Resolves nothing — it calls back. */
export async function openSessionMenu(anchor, { onPick, onNew }) {
  let sessions = [];
  try { sessions = await api.sessions(); } catch { /* server gone */ }
  const cur = store.convId;
  const rows = sessions.map((s) => `
    <button class="menu-item" data-conv="${esc(s.conv_id)}" title="${esc(oneLine(s.title, 200))}">
      <div class="mi-title"><span class="mi-name">${esc(oneLine(s.title, 90))}</span>${
        s.conv_id === cur ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-meta">${s.live ? "● live · " : ""}${esc(oneLine(s.model, 28))} ·
        ${s.message_count} msgs · ${relTime(s.mtime)}</div>
    </button>`).join("");
  const empty = `<div class="menu-note">No saved sessions in this project yet.</div>`;
  const m = menuAt(
    anchor,
    `<button class="menu-item" data-new><div class="mi-title">＋ New session</div>
       <div class="mi-desc">Start an empty conversation in this project.</div></button>
     <div class="menu-sep"></div>${rows || empty}`,
    { below: true },
  );
  m.addEventListener("click", (e) => {
    if (e.target.closest("[data-new]")) { m.closeMenu(); onNew(); return; }
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

// ---- settings ----

export function openSettings() {
  const m = modal(
    "Settings",
    `<div class="settings-layout">
      <nav class="settings-nav">
        <button data-page="general" class="active">General</button>
        <button data-page="appearance">Appearance</button>
        <button data-page="models">Models</button>
        <button data-page="plugins">Plugins</button>
      </nav>
      <div class="settings-content" id="settings-content"></div>
    </div>`
  );
  m.querySelector(".modal-body").style.padding = "0";
  const nav = m.querySelector(".settings-nav");
  nav.addEventListener("click", (e) => {
    const b = e.target.closest("[data-page]");
    if (!b) return;
    nav.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    renderPage(b.dataset.page);
  });
  renderPage("general");

  async function renderPage(page) {
    const c = m.querySelector("#settings-content");
    if (page === "general") {
      // Settings is reachable from Home, where no conversation has ever been
      // opened and nothing has filled store.bootstrap — fetch it on demand
      // (unscoped, so it describes the launch project).
      let bs = store.bootstrap;
      if (!bs) {
        c.innerHTML = `<div style="color:var(--fg-dim);font-size:13px">Loading…</div>`;
        try {
          bs = await api.bootstrap();
          store.bootstrap = bs;
        } catch {
          bs = {};
        }
      }
      c.innerHTML = `
        <div class="set-field"><label>Project</label>
          <input value="${esc(bs.cwd || "")}" disabled></div>
        <div class="set-field"><label>Provider endpoint (base URL)</label>
          <input id="set-baseurl" value="${esc(bs.base_url || "")}"></div>
        <div class="set-field"><label>API key ${bs.has_api_key
          ? '<span style="color:var(--success)">· saved</span>'
          : `<span style="color:var(--warning)">· not set (or $${esc(bs.api_key_env || "")})</span>`}</label>
          <input id="set-apikey" type="password" placeholder="sk-… (stored encrypted at rest)"></div>
        <div class="set-field"><label>Default permission mode (new sessions)</label>
          <select id="set-mode">${MODES.map(([id, t]) =>
            `<option value="${id}" ${bs.default_mode === id ? "selected" : ""}>${t}</option>`).join("")}
          </select></div>
        <button class="btn primary" id="set-save">Save</button>
        <span id="set-msg" style="margin-left:10px;font-size:12px;color:var(--fg-dim)"></span>`;
      c.querySelector("#set-save").addEventListener("click", async () => {
        const msg = c.querySelector("#set-msg");
        try {
          await api.putConfig({
            base_url: c.querySelector("#set-baseurl").value.trim(),
            default_mode: c.querySelector("#set-mode").value,
          });
          const key = c.querySelector("#set-apikey").value.trim();
          if (key) await api.putApiKey(key);
          msg.textContent = "Saved. New sessions pick this up.";
        } catch (err) { msg.textContent = "Save failed: " + err.message; }
      });
    } else if (page === "appearance") {
      // Presets arrive from the backend so the palettes live in one place.
      let bs = store.bootstrap;
      if (!bs) {
        try { bs = await api.bootstrap(); store.bootstrap = bs; } catch { bs = {}; }
      }
      const presets = bs.theme_presets || {};
      const current = bs.theme || {};
      const swatch = (colors) => ["background", "surface", "panel", "boost", "primary", "accent"]
        .map((k) => `<i style="background:${esc(colors[k] || "#000")}"></i>`).join("");
      const cards = Object.entries(presets).map(([name, colors]) => `
        <button class="theme-card" data-theme="${esc(name)}"
                ${colors.background === current.background ? 'data-current="1"' : ""}>
          <div class="tc-swatch">${swatch(colors)}</div>
          <div class="tc-name">${esc(name)}${
            colors.background === current.background ? '<span class="check">✓</span>' : ""}</div>
        </button>`).join("");
      c.innerHTML = `<div style="color:var(--fg-dim);font-size:13px;margin-bottom:12px">
          Surfaces stay neutral in the dark palettes — colour is reserved for
          what it marks. Picking one applies it immediately and saves it.</div>
        <div class="theme-grid">${cards || "<div>No presets available.</div>"}</div>
        <span id="theme-msg" style="font-size:12px;color:var(--fg-dim)"></span>`;
      c.querySelector(".theme-grid")?.addEventListener("click", async (e) => {
        const b = e.target.closest("[data-theme]");
        if (!b) return;
        const colors = presets[b.dataset.theme];
        applyTheme(colors);
        store.bootstrap = { ...(store.bootstrap || {}), theme: colors };
        const msg = c.querySelector("#theme-msg");
        try {
          await api.putConfig({ theme: colors });
          msg.textContent = `Saved “${b.dataset.theme}”.`;
          renderPage("appearance");
        } catch (err) { msg.textContent = "Save failed: " + err.message; }
      });
    } else if (page === "models") {
      c.innerHTML = `<div style="color:var(--fg-dim);font-size:13px;margin-bottom:10px">
        The session model is switched from the composer's model pill. This list
        comes from the provider's catalog.</div>
        <input id="set-model-filter" class="set-filter" spellcheck="false"
               placeholder="Filter models…" disabled>
        <div id="set-models" class="set-scroll">Loading…</div>`;
      const filter = c.querySelector("#set-model-filter");
      try {
        const models = await api.models();
        const card = (mo) => `
          <div class="plugin-card" style="margin-bottom:6px">
            <div class="p-name">${esc(mo.id)}
              ${store.state?.model === mo.id ? '<span class="p-badge">active</span>' : ""}</div>
            <div class="p-desc">ctx ${fmtTokens(mo.context_length)}
              ${mo.prompt_price != null ? ` · $${mo.prompt_price}/M in · $${mo.completion_price}/M out` : ""}</div>
          </div>`;
        // The whole catalog is listed; the filter is what keeps it usable.
        const paint = (list) => {
          c.querySelector("#set-models").innerHTML = list.length
            ? `<div class="set-count">${list.length} of ${models.length} models</div>`
              + list.map(card).join("")
            : `<div class="set-count">No model matches that filter.</div>`;
        };
        filter.disabled = false;
        filter.addEventListener("input", () => {
          const q = filter.value.trim().toLowerCase();
          paint(models.filter((mo) => (mo.id + " " + (mo.name || "")).toLowerCase().includes(q)));
        });
        paint(models);
      } catch (err) {
        c.querySelector("#set-models").textContent = "Could not load models: " + err.message;
      }
    } else if (page === "plugins") {
      c.innerHTML = `<div style="color:var(--fg-dim);font-size:13px;margin-bottom:10px">
        Agent capabilities are pluggable: tools (entry point <code>quickcode.tools</code>),
        providers (<code>quickcode.providers</code>), and MCP servers
        (<code>mcpServers</code> in .quickcode/settings.json).</div>
        <div id="set-plugins">Loading…</div>`;
      try {
        const inv = await api.plugins();
        const cards = inv.tools.map((t) => `
          <div class="plugin-card">
            <div class="p-name">${esc(t.name)}
              <span class="p-badge ${t.source === "mcp" ? "mcp" : ""}">${esc(t.source)}</span>
              ${t.read_only ? '<span class="p-badge ro">read-only</span>' : ""}</div>
            <div class="p-desc" title="${esc(t.description)}">${esc(t.description)}</div>
          </div>`).join("");
        const mcpNote = inv.mcp_servers.length
          ? `<div style="margin:10px 0 6px;font-size:12px;color:var(--fg-dim)">
               MCP servers connected: ${inv.mcp_servers.map(esc).join(", ")}</div>` : "";
        c.querySelector("#set-plugins").outerHTML =
          `${mcpNote}<div class="plugin-grid">${cards}</div>`;
      } catch (err) {
        c.querySelector("#set-plugins").textContent = "Could not load plugins: " + err.message;
      }
    }
  }
}
