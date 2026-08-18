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
import {
  PARTS, bodyHtml, canonicalHref, duplicateRefusal, sigilHtml, signatureOf,
} from "./kinds.js";
import { emptyFilterHtml, emptyHtml, wireEmpty } from "./empty.js";
import { partOfProblem, problemsCardHtml, wireProblems } from "./problems.js";
import { duplicatePlugin } from "./create/scaffold.js";

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

/** The card's right-hand column. Duplicate is offered on every plugin that has
 *  one, because duplicating *reads*, and reading was never restricted — a
 *  locked, required, built-in plugin is exactly the case the button exists for.
 *  Where there is nothing to copy into, the recourse takes its place rather
 *  than a button that would only ever explain itself by failing. */
function sideHtml(plugin, scope) {
  const refused = duplicateRefusal(plugin);
  return `<div class="k-card-side">
    <button class="ghost-btn" data-raw title="Show the raw definition">Raw</button>
    ${plugin.source === "authored"
      ? `<a class="ghost-btn" href="#/config/edit/${encodeURIComponent(plugin.id)}"
           title="Open the file — ${esc(scope === "user" ? "yours, in every project"
             : "in this project")}">Edit file</a>`
      : refused
        ? (refused.href
            ? `<a class="ghost-btn" href="${refused.href}" title="${esc(refused.why)}"
                 >${esc(refused.label)}</a>`
            : `<span class="k-nodup" title="${esc(refused.why)}">no copy</span>`)
        : `<button class="ghost-btn" data-dup title="Write an editable copy under
             .quickcode/plugins/ with derived_from set. The original is
             untouched.">⧉ Duplicate</button>`}
  </div>`;
}

function cardHtml(plugin, facts, scope = "") {
  return `<article class="k-card" data-id="${esc(plugin.id)}"
      data-kind="${esc(plugin.kind)}" data-tier="${esc(plugin.tier)}"
      ${plugin.enabled ? "" : "data-off"}>
    <a class="k-card-main" href="${canonicalHref(plugin)}">
      <div class="k-card-head">
        ${sigilHtml(plugin.kind)}
        <span class="k-title">${esc(plugin.title || plugin.id)}</span>
        <code class="k-id">${esc(plugin.id)}</code>
        <span class="k-badges">
          ${plugin.source === "authored"
            ? chip(scope === "user" ? "yours · every project" : "yours", "src-config") : ""}
          ${plugin.derived_from ? chip(`from ${plugin.derived_from}`) : ""}
          ${plugin.enabled ? "" : chip("off")}
          ${tierBadge(plugin.tier)}
        </span>
      </div>
      <div class="k-summary">${esc(summaryOf(plugin))}</div>
      ${bodyHtml(plugin, facts)}
    </a>
    ${sideHtml(plugin, scope)}
  </article>`;
}

/** id → the authored record, which is where `scope` actually lives. A plugin's
 *  metadata carries it too for the kinds that have metadata, but the authored
 *  list is the one answer that covers every kind. */
function scopeIndex(ctx) {
  const out = {};
  for (const p of ctx.authored || []) out[p.id] = p.scope || "";
  return out;
}

const SOURCE_FILTERS = [
  ["", "All"],
  ["authored", "Yours"],
  ["builtin", "Built in"],
];

const SCOPE_FILTERS = [
  ["", "Both scopes"],
  ["project", "This project"],
  ["user", "Every project"],
];

function filterBar(state, counts) {
  return `<div class="cfg-filters">
    <div class="cfg-filter-group" role="group" aria-label="Source">
      ${SOURCE_FILTERS.map(([v, label]) => `<button class="cfg-fchip${
        state.source === v ? " on" : ""}" data-source="${esc(v)}">${esc(label)}${
        v === "authored" && counts.authored ? ` <span class="cfg-fcount">${counts.authored}</span>` : ""
      }</button>`).join("")}
    </div>
    <div class="cfg-filter-group${state.source === "builtin" ? " off" : ""}"
         role="group" aria-label="Scope">
      ${SCOPE_FILTERS.map(([v, label]) => `<button class="cfg-fchip${
        state.scope === v ? " on" : ""}" data-scope="${esc(v)}"${
        state.source === "builtin" ? " disabled" : ""}>${esc(label)}</button>`).join("")}
    </div>
    <span class="cfg-filter-note-inline">Scope is where the file lives:
      <code>.quickcode/plugins/</code> travels with the repository,
      <code>~/.quickcode/plugins/</code> follows you.</span>
  </div>`;
}

export async function renderParts(host, ctx, slug, query = {}) {
  const part = PARTS.find((p) => p.slug === slug) || PARTS[0];
  const all = ctx.kernel.plugins.filter((p) => part.kinds.includes(p.kind));
  const scopes = scopeIndex(ctx);
  const state = {
    q: query.q || "", server: query.server || "",
    source: query.source || "", scope: query.scope || "",
  };
  const counts = { authored: all.filter((p) => p.source === "authored").length };
  // A problem whose plugin was skipped has no card to sit on, which is exactly
  // why the card is pinned above the list rather than attached to a row.
  //
  // Problems that name no plugin — a file whose `kind:` could not be read, a
  // project whose command tools are inert because it is untrusted — are shown
  // on *every* Parts page. They are precisely the ones that cannot be placed,
  // and the failure this card exists to prevent is a plugin disappearing with
  // its reason filed somewhere the user was not looking.
  const mine = (ctx.kernel.problems || []).filter((p) => {
    const where = partOfProblem(p);
    return where === part.slug || where === "";
  });
  const elsewhere = (ctx.kernel.problems || []).length - mine.length;

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
    <div class="pb-slot">${problemsCardHtml(mine, {
      title: `Problems on this page`,
      note: elsewhere ? `${elsewhere} more elsewhere —
        <a class="k-link" href="#/config/problems">all problems →</a>` : "",
    })}</div>
    ${state.server ? `<div class="cfg-filter-note">Filtered to the tools
      <code>${esc(state.server)}</code> contributed.
      <a class="k-link" href="#/config/parts/${esc(part.slug)}">clear</a></div>` : ""}
    ${filterBar(state, counts)}
    <input class="set-filter cfg-part-filter" type="search" spellcheck="false"
           placeholder="Filter ${esc(part.title.toLowerCase())}…" value="${esc(state.q)}">
    <div class="k-list"></div>
  </div>`;

  const list = host.querySelector(".k-list");
  const filter = host.querySelector(".cfg-part-filter");
  const dup = (id, btn) => duplicatePlugin(ctx, id, btn);

  const matches = () => {
    const q = state.q.trim().toLowerCase();
    return all.filter((p) => {
      if (state.server && !p.id.startsWith(`tool.mcp__${state.server}__`)) return false;
      if (state.source === "authored" && p.source !== "authored") return false;
      if (state.source === "builtin" && p.source === "authored") return false;
      if (state.scope && (scopes[p.id] || p.metadata?.scope) !== state.scope) return false;
      if (!q) return true;
      return `${p.id} ${p.title} ${p.description} ${p.group}`.toLowerCase().includes(q);
    });
  };

  const paint = () => {
    const rows = matches();
    const filtered = state.q.trim() || state.source || state.scope || state.server;
    list.innerHTML = rows.length
      ? rows.map((p) => cardHtml(p, ctx.facts, scopes[p.id] || p.metadata?.scope || "")).join("")
      : filtered
        ? emptyFilterHtml(part.slug, { source: state.source, scope: state.scope })
        : emptyHtml(part.slug);
  };

  paint();
  filter.addEventListener("input", () => { state.q = filter.value; paint(); });
  host.querySelector(".cfg-filters").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-source], [data-scope]");
    if (!btn || btn.disabled) return;
    if ("source" in btn.dataset) {
      state.source = btn.dataset.source;
      if (state.source === "builtin") state.scope = "";
    } else state.scope = btn.dataset.scope;
    // A full re-render rather than a repaint: the filter bar, the counts and
    // the empty state all move together, and one path that always produces the
    // whole page cannot leave two of them disagreeing.
    renderParts(host, ctx, slug, { ...query, ...state });
  });
  wireProblems(host, ctx);
  wireEmpty(list, dup);
  list.addEventListener("click", (e) => {
    const card = e.target.closest(".k-card");
    if (!card) return;
    if (e.target.closest("[data-dup]")) {
      e.preventDefault();
      dup(card.dataset.id, e.target.closest("[data-dup]"));
      return;
    }
    if (!e.target.closest("[data-raw]")) return;
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
