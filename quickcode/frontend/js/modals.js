// Modals and dropdown menus: permission review, plan review, mode menu,
// model picker, sessions, settings (General / Models / Plugins).

import { api } from "./api.js";
import { store, subscribe } from "./store.js";
import { actions } from "./ws.js";
import { applyTheme, el, esc, fmtTokens, oneLine, relTime } from "./util.js";

const root = () => document.getElementById("modal-root");

function closeModal() { root().innerHTML = ""; }

function modal(title, bodyHtml, footHtml = "") {
  closeModal();
  const m = el(`<div class="modal-backdrop"><div class="modal">
    <div class="modal-head"><span>${title}</span>
      <button class="ghost-btn" data-close>✕</button></div>
    <div class="modal-body">${bodyHtml}</div>
    ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
  </div></div>`);
  m.addEventListener("click", (e) => {
    if (e.target === m || e.target.closest("[data-close]")) closeModal();
  });
  root().appendChild(m);
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

function isPendingNow(reqId) {
  return (store.state?.pending || []).some((p) => p.req_id === reqId) || !store.replaying;
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

function menuAt(anchor, contentHtml, { searchable = false, below = false } = {}) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const m = el(`<div class="menu">
    ${searchable ? '<input class="menu-search" placeholder="Search…">' : ""}
    <div class="menu-list">${contentHtml}</div></div>`);
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const mh = Math.min(m.offsetHeight, window.innerHeight * 0.6);
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + "px";
  // Composer pills open upward; a top-bar anchor has no room above it.
  m.style.top = below
    ? Math.min(r.bottom + 8, window.innerHeight - mh - 8) + "px"
    : Math.max(8, r.top - mh - 8) + "px";
  const dismiss = (e) => {
    if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", dismiss); }
  };
  setTimeout(() => document.addEventListener("mousedown", dismiss), 0);
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
    if (b) { actions.setMode(b.dataset.mode); m.remove(); }
  });
}

export async function openModelMenu(anchor) {
  let models = [];
  try { models = await api.models(); } catch { /* offline */ }
  const cur = store.state?.model;
  const render = (list) => list.slice(0, 200).map((mo) => `
    <button class="menu-item" data-model="${esc(mo.id)}">
      <div class="mi-title">${esc(mo.name || mo.id)}${cur === mo.id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-meta">${esc(mo.id)} · ctx ${fmtTokens(mo.context_length)}${
        mo.prompt_price != null ? ` · $${mo.prompt_price}/M in` : ""}</div>
    </button>`).join("");
  const m = menuAt(anchor, render(models), { searchable: true });
  const list = m.querySelector(".menu-list");
  const search = m.querySelector(".menu-search");
  search?.focus();
  search?.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    list.innerHTML = render(models.filter((mo) =>
      (mo.id + " " + mo.name).toLowerCase().includes(q)));
  });
  m.addEventListener("click", (e) => {
    const b = e.target.closest("[data-model]");
    if (b) { actions.setModel(b.dataset.model); m.remove(); }
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
    <button class="menu-item" data-conv="${esc(s.conv_id)}">
      <div class="mi-title">${esc(oneLine(s.title, 60))}
        ${s.conv_id === cur ? '<span class="check">✓</span>' : ""}</div>
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
    if (e.target.closest("[data-new]")) { m.remove(); onNew(); return; }
    const b = e.target.closest("[data-conv]");
    if (b) { m.remove(); onPick(b.dataset.conv); }
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
        comes from the provider's catalog.</div><div id="set-models">Loading…</div>`;
      try {
        const models = await api.models();
        c.querySelector("#set-models").innerHTML = models.slice(0, 100).map((mo) => `
          <div class="plugin-card" style="margin-bottom:6px">
            <div class="p-name">${esc(mo.id)}
              ${store.state?.model === mo.id ? '<span class="p-badge">active</span>' : ""}</div>
            <div class="p-desc">ctx ${fmtTokens(mo.context_length)}
              ${mo.prompt_price != null ? ` · $${mo.prompt_price}/M in · $${mo.completion_price}/M out` : ""}</div>
          </div>`).join("");
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
