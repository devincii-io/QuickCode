// Agents — the section the navigation leads with, because "make it stop
// rewriting my whole file" is a sentence about an agent.
//
// The index is here; every agent page — `@orchestrator` included, as a
// first-class agent rather than a special case — is the workbench in
// `config/agent.js`, which is the same component from both entry points.

import { esc } from "../util.js";
import { chip, openPluginView, tierBadge } from "../settings/ui.js";
import { summaryOf } from "./explain.js";
import { renderWorkbench } from "./agent.js";
import { bodyHtml, sigilHtml } from "./kinds.js";
import { duplicatePlugin } from "./create/scaffold.js";

export const ORCHESTRATOR = "@orchestrator";

export function agentPlugins(kernel) {
  return kernel.plugins.filter((p) => p.kind === "agent");
}

export function renderAgentsIndex(host, ctx) {
  const agents = agentPlugins(ctx.kernel);
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">Agents</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="agent">@</span>
        <h2>Agents</h2>
        <span class="cfg-count">${agents.length + 1}</span>
        <span class="cfg-head-actions">
          <a class="btn" href="#/config/new/agent">+ New agent</a>
        </span>
      </div>
    </header>
    <div class="cfg-lede">One page per agent: who it is, what it may call, which
      models it may run on, and how far it may go before it stops. The
      orchestrator is the agent you talk to; the others are spawnable by it and
      never ask you for permission themselves — their ceiling is the whole of
      their answer to a permission question.</div>
    <div class="k-list">
      <article class="k-card" data-kind="agent" data-tier="free">
        <a class="k-card-main" href="#/config/agents/${encodeURIComponent(ORCHESTRATOR)}">
          <div class="k-card-head">
            ${sigilHtml("agent")}
            <span class="k-title">Orchestrator</span>
            <code class="k-id">@orchestrator</code>
            <span class="k-badges">${chip("you talk to this one")}</span>
          </div>
          <div class="k-summary">The agent a session starts as. Its tools, prompt
            and starting mode come from the active composition.</div>
          <div class="k-facts">
            <span class="k-fact mono">${esc(ctx.presets?.active || "standard")}</span>
            <span class="k-fact">${ctx.prompt ? `${ctx.prompt.sections.length} prompt sections`
              : "prompt unavailable"}</span>
          </div>
        </a>
      </article>
      ${agents.map((p) => `
        <article class="k-card" data-kind="agent" data-tier="${esc(p.tier)}"
                 data-id="${esc(p.id)}">
          <a class="k-card-main" href="#/config/agents/${encodeURIComponent(p.id)}">
            <div class="k-card-head">
              ${sigilHtml("agent")}
              <span class="k-title">${esc(p.title)}</span>
              <code class="k-id">${esc(p.id)}</code>
              <span class="k-badges">
                ${chip(p.metadata?.builtin ? "built-in" : "authored",
                       p.metadata?.builtin ? "" : "src-config")}
                ${tierBadge(p.tier)}</span>
            </div>
            <div class="k-summary">${esc(summaryOf(p))}</div>
            ${bodyHtml(p, ctx.facts)}
          </a>
          <div class="k-card-side">
            <button class="ghost-btn" data-raw title="Read its instructions">Raw</button>
            ${p.metadata?.builtin
              ? `<button class="ghost-btn" data-dup title="Write an editable
                   markdown copy of this agent under .quickcode/plugins/ with
                   derived_from set. Every line that is fixed here becomes plain
                   text there; this one is untouched and stays available."
                   >⧉ Duplicate</button>`
              : `<a class="ghost-btn" href="#/config/edit/${encodeURIComponent(p.id)}"
                   title="Open the file this agent is">Edit file</a>`}
          </div>
        </article>`).join("")}
    </div>
  </div>`;

  // Duplicate is offered on the index for the same reason the workbench offers
  // it: duplicating *reads*, and reading was never restricted, so a built-in
  // agent is exactly the case the button exists for. A refusal — the server's,
  // never a guess made here — replaces the button with its reason and its
  // recourse, which is what `parts.js` and `detail.js` do.
  host.querySelector(".k-list").addEventListener("click", (e) => {
    const card = e.target.closest(".k-card");
    if (!card) return;
    const dup = e.target.closest("[data-dup]");
    if (dup) {
      e.preventDefault();
      duplicatePlugin(ctx, card.dataset.id, dup);
      return;
    }
    if (!e.target.closest("[data-raw]")) return;
    e.preventDefault();
    openPluginView(ctx.api, ctx.kernel.plugins.find((p) => p.id === card.dataset.id));
  });
}

export async function renderAgent(host, ctx, id, query = {}) {
  // The rail and the cards address agents by plugin id (`agent.explore`); the
  // kernel addresses them by agent id (`explore`). Both land here, because a
  // link that has been in someone's address bar since Phase 3 must keep
  // working.
  const agentId = id.startsWith("agent.") ? id.slice("agent.".length) : id;
  // One component, both entry points: the orchestrator and a spawnable agent
  // render the same six sections against different backing stores. The
  // difference is what the identity header says and whether Delegation has
  // anything in it — everything else is literally the same code.
  await renderWorkbench(host, ctx, agentId, query);
}
