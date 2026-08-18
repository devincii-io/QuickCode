// #view-config — configuration as a real third view, peer of Home and the
// workspace, not a modal.
//
// Why a view: the workbench is three columns and does not fit in a dialog;
// every page here deserves a URL (a trajectory event should be able to link to
// the agent that produced it); the creation flows are multi-step with a live
// preview. And the reason that matters most in practice — **showing this must
// not disconnect the workspace socket**. Leaving the chat to change a setting
// and losing the session is exactly the friction this exists to remove, so
// main.js's showConfig() never calls disconnect(); the workspace stays mounted
// and hidden, the way it already is while Home shows.
//
// One thing stays a modal: quick settings (endpoint, key, theme) on the
// composer, for the three install-level things people change mid-conversation.

import { esc } from "../util.js";
import { store } from "../store.js";
import { renderAgent, renderAgentsIndex } from "./agents.js";
import { renderCompositions } from "./compositions.js";
import { renderDetail } from "./detail.js";
import { renderInstall } from "./install.js";
import { renderMachineRoom } from "./machineroom.js";
import { renderParts } from "./parts.js";
import { renderProblems } from "./problems.js";
import { renderRail } from "./rail.js";
import { renderEditor, renderNew } from "./create/scaffold.js";
import { PARTS, canonicalHref, kindLabel, sigilHtml } from "./kinds.js";
import { summaryOf } from "./explain.js";

const $ = (id) => document.getElementById(id);

let ctx = null;          // { api, kernel, presets, prompt, facts, … }
let loading = null;      // in-flight load, so two navigations share one fetch
let pendingHighlight = null;
let lastRoute = "#/config/agents";
let wired = false;

export const DEFAULT_ROUTE = "#/config/agents";

export function isConfigRoute(hash) {
  return String(hash || "").startsWith("#/config");
}

/** `#/config/parts/tools/tool.bash?q=x` → {path:[…], query:{…}} */
export function parseRoute(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const path = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  if (path[0] === "config") path.shift();
  const query = {};
  for (const [k, v] of new URLSearchParams(queryPart || "")) query[k] = v;
  return { path: path.length ? path : ["agents"], query };
}

export function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

// ---- data -----------------------------------------------------------------

async function load(api) {
  const kernel = await api.kernel();
  // The prompt and the preset list are what make the cards say something a
  // person can use. Neither is fatal: a page that cannot read them says so in
  // the one place it would have used them.
  // `authored` is the list of files the user owns, with the two directory
  // paths. It is what the Yours filter, the scope filter and the editor's
  // header read; a page that cannot get it degrades to "nothing is yours yet",
  // which is the same thing an install with no authored files shows.
  const [presets, prompt, authored] = await Promise.all([
    api.presets().catch(() => null),
    api.prompt().catch(() => null),
    api.authored().catch(() => null),
  ]);

  const ranges = {};
  for (const s of prompt?.sections || []) ranges[s.id] = { start: s.start, end: s.end };
  const mcpTools = {};
  for (const p of kernel.plugins) {
    if (p.kind !== "mcp_server") continue;
    const server = p.metadata?.server || p.title;
    mcpTools[p.id] = kernel.plugins.filter(
      (t) => t.kind === "tool" && t.id.startsWith(`tool.mcp__${server}__`)).length;
  }
  // The authoring routes and the kernel both report problems; they are the
  // same array at two different times, so they are merged and de-duplicated
  // here rather than shown as two lists that mostly agree.
  kernel.problems = kernel.problems || [];
  const seen = new Set(kernel.problems.map(problemKey));
  for (const p of authored?.problems || []) {
    if (!seen.has(problemKey(p))) { kernel.problems.push(p); seen.add(problemKey(p)); }
  }

  return {
    api, kernel, presets, prompt,
    authored: authored?.plugins || [],
    dirs: authored?.dirs || {},
    facts: {
      schemas: {}, ranges, mcpTools,
      connected: kernel.mcp_servers || [],
      endpoint: store.bootstrap?.base_url || "",
      modelCount: null,
    },
    go,
    touched: () => {},        // a plugin changed; the cached kernel copy is live
    // A file was written. The cached kernel copy is now a description of a
    // configuration that no longer exists, so it is dropped rather than
    // patched: the next render re-reads, and the list the UI shows stays the
    // list the runtime would build.
    invalidate,
    railDirty: () => renderRail($("cfg-rail"), ctx, parseRoute(location.hash)),
  };
}

function problemKey(p) {
  return `${p.code}|${p.subject}|${p.message}`;
}

export function invalidate() { ctx = null; loading = null; }

// ---- rendering ------------------------------------------------------------

export async function render() {
  const page = $("cfg-page");
  if (!page) return;
  const route = parseRoute(location.hash);
  lastRoute = isConfigRoute(location.hash) ? location.hash : lastRoute;

  if (!ctx) {
    page.innerHTML = `<div class="cfg-page-inner"><div class="set-loading">Reading the
      plugin kernel…</div></div>`;
    try {
      loading = loading || load(currentApi);
      ctx = await loading;
      loading = null;
    } catch (err) {
      loading = null;
      page.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
        read the plugin kernel: ${esc(err.message)}<br>Everything here describes
        what the runtime actually runs, so nothing is shown rather than
        something invented.</div></div>`;
      return;
    }
  }

  renderRail($("cfg-rail"), ctx, route);
  page.scrollTop = 0;
  const [head, a, b] = route.path;

  try {
    // `?conv=` opens a running session's frozen view and `?preset=` resolves
    // against a composition other than the active one, so both ride the URL:
    // "the agent this session is actually running" has to be linkable.
    if (head === "agents" && a) await renderAgent(page, ctx, a, route.query);
    else if (head === "agents") renderAgentsIndex(page, ctx);
    else if (head === "compositions") renderCompositions(page, ctx, a || "");
    else if (head === "parts" && b) {
      const part = PARTS.find((p) => p.slug === a);
      const plugin = ctx.kernel.plugins.find((p) => p.id === b);
      await renderPluginPage(page, b,
        `<a href="#/config/parts/${esc(a)}">Parts ▸ ${esc(part?.title || a)}</a>`,
        plugin && plugin.tier === "locked" && a === "policies"
          ? `Also listed in the <a class="k-link" href="#/config/machine-room">Machine
             room</a>, which is the filter over everything that is fixed. This is
             its one page either way.`
          : "");
    }
    else if (head === "parts") await renderParts(page, ctx, a || "tools", route.query);
    else if (head === "machine-room" && a) {
      // One canonical page per plugin, always: the Machine room is a filter
      // over the locked ones, not a second address for them.
      const plugin = ctx.kernel.plugins.find((p) => p.id === a);
      go(plugin ? canonicalHref(plugin) : "#/config/machine-room");
      return;
    } else if (head === "machine-room") renderMachineRoom(page, ctx);
    else if (head === "install") await renderInstall(page, ctx, a || "general");
    else if (head === "new") renderNew(page, ctx, a || "agent");
    // The raw source editor. It has a URL because it is a page you link people
    // to — "the file that does this is here" — not a dialog over a list.
    else if (head === "edit" && a) await renderEditor(page, ctx, a, route.query);
    else if (head === "problems") renderProblems(page, ctx);
    else renderAgentsIndex(page, ctx);
  } catch (err) {
    page.innerHTML = `<div class="cfg-page-inner"><div class="set-error">This page
      failed to render: ${esc(err.message)}</div></div>`;
    return;
  }
  applyHighlight(page);
}

async function renderPluginPage(page, id, crumb, lede = "") {
  const plugin = ctx.kernel.plugins.find((p) => p.id === id);
  if (!plugin) {
    page.innerHTML = `<div class="cfg-page-inner"><div class="set-error">There is no
      plugin <code>${esc(id)}</code> in this project.</div></div>`;
    return;
  }
  const inner = document.createElement("div");
  inner.className = "cfg-page-inner";
  page.innerHTML = "";
  page.appendChild(inner);
  await renderDetail(inner, ctx, plugin, { crumb, lede });
}

function applyHighlight(page) {
  if (!pendingHighlight) return;
  const { id, key } = pendingHighlight;
  pendingHighlight = null;
  if (!key) return;
  const node = page.querySelector(`.set-f[data-key="${CSS.escape(key)}"]`)
    || [...page.querySelectorAll(".k-fixed-row")].find(
      (r) => r.querySelector(".k-fixed-key")?.textContent === key);
  if (!node || !id) return;
  node.classList.add("hl");
  node.scrollIntoView({ block: "center" });
  setTimeout(() => node.classList.remove("hl"), 2400);
}

// ---- the header search box ------------------------------------------------
//
// The flat 37-row list is retired as a page and lives here instead: a lookup
// instrument belongs behind a search box, not in the navigation where it
// competes with the pages that teach.

function searchAll(q) {
  const needle = q.trim().toLowerCase();
  if (!needle || !ctx) return [];
  const out = [];
  for (const p of ctx.kernel.plugins) {
    const hay = `${p.id} ${p.title} ${p.description} ${p.group} ${p.kind}`.toLowerCase();
    if (hay.includes(needle)) {
      out.push({ kind: p.kind, title: p.title, id: p.id, sub: summaryOf(p),
                 href: canonicalHref(p), plugin: p });
    }
    for (const s of p.settings || []) {
      const shay = `${s.key} ${s.title} ${s.help}`.toLowerCase();
      if (!shay.includes(needle)) continue;
      out.push({ kind: p.kind, title: `${s.title || s.key}`, id: `${p.id}.${s.key}`,
                 sub: `setting on ${p.title} · ${s.tier}`,
                 href: canonicalHref(p), plugin: p, key: s.key });
    }
  }
  for (const p of ctx.presets?.presets || []) {
    if (`${p.id} ${p.title} ${p.description}`.toLowerCase().includes(needle)) {
      out.push({ kind: "agent", title: p.title, id: p.id,
                 sub: "composition", href: `#/config/compositions/${encodeURIComponent(p.id)}` });
    }
  }
  return out.slice(0, 40);
}

function paintResults(box, rows, q) {
  if (!q.trim()) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = rows.length
    ? rows.map((r, i) => `<a class="cfg-result${i === 0 ? " first" : ""}"
        href="${r.href}" data-key="${esc(r.key || "")}" data-id="${esc(r.plugin?.id || "")}">
        ${sigilHtml(r.kind)}
        <span class="cr-title">${esc(r.title)}</span>
        <code class="cr-id">${esc(r.id)}</code>
        <span class="cr-sub">${esc(r.sub || kindLabel(r.kind))}</span>
      </a>`).join("")
    : `<div class="cfg-result empty">Nothing matches “${esc(q)}”.</div>`;
}

// ---- wiring ---------------------------------------------------------------

let currentApi = null;

export function initConfig({ api, onDone }) {
  currentApi = api;
  if (wired) return;
  wired = true;

  const search = $("cfg-search");
  const results = $("cfg-results");

  $("cfg-done").addEventListener("click", onDone);
  $("cfg-reload").addEventListener("click", () => { invalidate(); render(); });

  const runSearch = () => paintResults(results, searchAll(search.value), search.value);
  search.addEventListener("input", runSearch);
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      search.value = ""; paintResults(results, [], "");
      search.blur();
      return;
    }
    if (e.key !== "Enter") return;
    const first = results.querySelector(".cfg-result[href]");
    if (first) first.click();
  });
  results.addEventListener("click", (e) => {
    const row = e.target.closest(".cfg-result[href]");
    if (!row) return;
    if (row.dataset.id) pendingHighlight = { id: row.dataset.id, key: row.dataset.key };
    search.value = "";
    paintResults(results, [], "");
    // The href does the navigation and the hashchange listener renders — except
    // when the result is on the page already, where nothing would fire.
    if (row.getAttribute("href") === location.hash) applyHighlight($("cfg-page"));
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".cfg-search-wrap")) paintResults(results, [], "");
  });
}

export function lastConfigRoute() { return lastRoute; }
