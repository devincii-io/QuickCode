// Parts: the plugin inventory, split by kind.
//
// Kind rather than `spec.group`, because kind is the only grouping that
// predicts what a page looks like — a prompt section shows its first lines and
// its byte range, a tool shows its signature, an MCP server shows the command
// it runs. The flat 37-row list this replaces is retired as a page and lives
// on as the header search box, because a flat list is a lookup instrument and
// lookup belongs behind search rather than in the navigation.

import { esc } from "../util.js";
import { chip, openPluginView, tierBadge } from "../settings/ui.js";
import { summaryOf } from "./explain.js";
import { PARTS, bodyHtml, canonicalHref, sigilHtml, signatureOf } from "./kinds.js";

const LEDE = {
  tools: `Everything the model can call. Read-only tools skip the permission
    prompt and run in parallel with each other, which is the single most
    consequential fact about a tool — so it is on every card.`,
  prompt: `The system prompt is not one string: it is these sections, composed
    in order. Each one is a plugin, and each card shows where its text actually
    lands in the prompt the next session starts from.`,
  models: `Where tokens come from. Providers are discovered from Python entry
    points (<code>quickcode.providers</code>), so there is nothing to author
    here — the endpoint and the key are under Install.`,
  mcp: `External MCP processes. Each configured server is a plugin, and the
    tools it contributes appear on the Tools page alongside the built-in ones.`,
  policies: `The rules and limits the loop runs under: permission modes, the
    fan-out caps, the compaction trigger, the session log. Several of these
    have settings that are fixed by design; they are readable in full and
    linked from the Machine room.`,
};

const NEW_ACTION = {
  tools: ["+ New command tool", "#/config/new/tool"],
  prompt: ["+ New prompt section", "#/config/new/prompt"],
};

function cardHtml(plugin, facts) {
  return `<article class="k-card" data-id="${esc(plugin.id)}"
      data-kind="${esc(plugin.kind)}" data-tier="${esc(plugin.tier)}"
      ${plugin.enabled ? "" : "data-off"}>
    <a class="k-card-main" href="${canonicalHref(plugin)}">
      <div class="k-card-head">
        ${sigilHtml(plugin.kind)}
        <span class="k-title">${esc(plugin.title || plugin.id)}</span>
        <code class="k-id">${esc(plugin.id)}</code>
        <span class="k-badges">
          ${plugin.enabled ? "" : chip("off")}
          ${tierBadge(plugin.tier)}
        </span>
      </div>
      <div class="k-summary">${esc(summaryOf(plugin))}</div>
      ${bodyHtml(plugin, facts)}
    </a>
    <div class="k-card-side">
      <button class="ghost-btn" data-raw title="Show the raw definition">Raw</button>
    </div>
  </article>`;
}

function emptyHtml(slug, ctx) {
  // Every empty state names one real thing that already exists, says in one
  // sentence what you would change about it, and offers a way in.
  if (slug === "mcp") {
    return `<div class="set-empty">
      <p>No MCP servers configured.</p>
      <p>An MCP server is an external process that contributes tools. Paste a
        Claude-style block under <code>mcpServers</code> in
        <code>.quickcode/settings.json</code> and it shows up here as a plugin,
        with its command readable and its tools listed on the Tools page.</p>
    </div>`;
  }
  return `<div class="set-empty">Nothing of this kind is registered in
    ${esc(ctx.kernel ? "this project" : "this install")}.</div>`;
}

export async function renderParts(host, ctx, slug, query = {}) {
  const part = PARTS.find((p) => p.slug === slug) || PARTS[0];
  const all = ctx.kernel.plugins.filter((p) => part.kinds.includes(p.kind));
  const state = { q: query.q || "", server: query.server || "" };

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs"><a href="#/config/parts/${esc(part.slug)}">Parts</a> ▸
        ${esc(part.title)}</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="${esc(part.kinds[0])}">${esc(part.sigil)}</span>
        <h2>${esc(part.title)}</h2>
        <span class="cfg-count">${all.length}</span>
        <span class="cfg-head-actions">
          ${NEW_ACTION[part.slug]
            ? `<a class="btn" href="${NEW_ACTION[part.slug][1]}">${esc(NEW_ACTION[part.slug][0])}</a>`
            : ""}
        </span>
      </div>
    </header>
    <div class="cfg-lede">${LEDE[part.slug] || ""}</div>
    ${state.server ? `<div class="cfg-filter-note">Filtered to the tools
      <code>${esc(state.server)}</code> contributed.
      <a class="k-link" href="#/config/parts/${esc(part.slug)}">clear</a></div>` : ""}
    <input class="set-filter cfg-part-filter" type="search" spellcheck="false"
           placeholder="Filter ${esc(part.title.toLowerCase())}…" value="${esc(state.q)}">
    <div class="k-list"></div>
  </div>`;

  const list = host.querySelector(".k-list");
  const filter = host.querySelector(".cfg-part-filter");

  const matches = () => {
    const q = state.q.trim().toLowerCase();
    return all.filter((p) => {
      if (state.server && !p.id.startsWith(`tool.mcp__${state.server}__`)) return false;
      if (!q) return true;
      return `${p.id} ${p.title} ${p.description} ${p.group}`.toLowerCase().includes(q);
    });
  };

  const paint = () => {
    const rows = matches();
    list.innerHTML = rows.length
      ? rows.map((p) => cardHtml(p, ctx.facts)).join("")
      : all.length
        ? `<div class="set-empty">Nothing matches that.</div>`
        : emptyHtml(part.slug, ctx);
  };

  paint();
  filter.addEventListener("input", () => { state.q = filter.value; paint(); });
  list.addEventListener("click", (e) => {
    const card = e.target.closest(".k-card");
    if (!card || !e.target.closest("[data-raw]")) return;
    e.preventDefault();
    openPluginView(ctx.api, ctx.kernel.plugins.find((p) => p.id === card.dataset.id));
  });

  // Tools show their real signature, which means reading each declaration.
  // Done after the first paint and cached on ctx, so it costs one pass per
  // configuration session rather than one per visit.
  if (part.slug === "tools") await fillSignatures(ctx, all, list);
}

async function fillSignatures(ctx, tools, list) {
  const missing = tools.filter((t) => ctx.facts.schemas[t.id] === undefined);
  if (!missing.length) { repaintSignatures(ctx, list); return; }
  await Promise.all(missing.map(async (t) => {
    try {
      const detail = await ctx.api.plugin(t.id);
      ctx.facts.schemas[t.id] = signatureOf(detail.view?.content || "", t.title) || "";
    } catch {
      ctx.facts.schemas[t.id] = "";     // an unreadable schema stays blank, not wrong
    }
  }));
  repaintSignatures(ctx, list);
}

function repaintSignatures(ctx, list) {
  for (const card of list.querySelectorAll(".k-card")) {
    const sig = ctx.facts.schemas[card.dataset.id];
    const slot = card.querySelector(".k-body");
    if (sig && slot) slot.textContent = sig;
  }
}
