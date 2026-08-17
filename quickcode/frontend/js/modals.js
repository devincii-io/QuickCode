// Modals and dropdown menus: permission review, plan review, mode menu,
// model picker, sessions, settings (General / Models / Plugins).

import { api } from "./api.js";
import { store, subscribe } from "./store.js";
import { actions } from "./ws.js";
import { el, esc, fmtTokens, oneLine, relTime } from "./util.js";

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

function menuAt(anchor, contentHtml, { searchable = false } = {}) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const m = el(`<div class="menu">
    ${searchable ? '<input class="menu-search" placeholder="Search…">' : ""}
    <div class="menu-list">${contentHtml}</div></div>`);
  document.body.appendChild(m);
  const r = anchor.getBoundingClientRect();
  const mh = Math.min(m.offsetHeight, window.innerHeight * 0.6);
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + "px";
  m.style.top = Math.max(8, r.top - mh - 8) + "px";
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

// ---- sessions ----

export async function openSessions(onPick) {
  let sessions = [];
  try { sessions = await api.sessions(); } catch { /* server gone */ }
  const rows = sessions.length ? sessions.map((s) => `
    <button class="session-row" data-conv="${esc(s.conv_id)}">
      <span class="s-title">${esc(s.title)}</span>
      ${s.live ? '<span class="s-live">● live</span>' : ""}
      <span class="s-meta">${esc(oneLine(s.model, 30))} · ${s.message_count} msgs · ${relTime(s.mtime)}</span>
    </button>`).join("")
    : `<div style="color:var(--fg-dim)">No sessions in this project yet.</div>`;
  const m = modal("Sessions", rows);
  m.addEventListener("click", (e) => {
    const b = e.target.closest("[data-conv]");
    if (b) { closeModal(); onPick(b.dataset.conv); }
  });
}

// ---- settings ----

export function openSettings() {
  const m = modal(
    "Settings",
    `<div class="settings-layout">
      <nav class="settings-nav">
        <button data-page="general" class="active">General</button>
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
      const bs = store.bootstrap || {};
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
