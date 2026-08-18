// Agents — the section the navigation leads with, because "make it stop
// rewriting my whole file" is a sentence about an agent.
//
// The three-column workbench (editor beside a live preview of the exact bytes
// the agent will receive) is Phase 4 and needs the `/resolved` and `/preview`
// endpoints that do not exist yet. What is here now is the part that does not:
// each agent's identity, its model policy, its tool grant and its ceiling, all
// editable through the same tiered PUT as any other plugin, plus an honest
// panel saying what the workbench will add and why it is not here yet.

import { esc } from "../util.js";
import { chip, openPluginView, tierBadge } from "../settings/ui.js";
import { summaryOf } from "./explain.js";
import { renderDetail } from "./detail.js";
import { bodyHtml, sigilHtml } from "./kinds.js";

export const ORCHESTRATOR = "@orchestrator";

export function agentPlugins(kernel) {
  return kernel.plugins.filter((p) => p.kind === "agent");
}

function workbenchNote(what) {
  return `<section class="cfg-soon">
    <h4>Workbench — next pass</h4>
    <p>${what}</p>
    <p class="cfg-note">It needs two routes the kernel does not expose yet:
      <code>GET /api/kernel/agents/{id}/resolved</code> for the composed prompt
      with its section boundaries and the exact tool schemas, and
      <code>POST /api/kernel/agents/{id}/preview</code> so an unsaved draft can
      be previewed without the browser re-implementing the prompt composer in a
      second language. Until they land, nothing here pretends to know bytes it
      cannot see.</p>
  </section>`;
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
          </div>
        </article>`).join("")}
    </div>
  </div>`;

  host.querySelector(".k-list").addEventListener("click", (e) => {
    const card = e.target.closest(".k-card");
    if (!card || !e.target.closest("[data-raw]")) return;
    e.preventDefault();
    openPluginView(ctx.api, ctx.kernel.plugins.find((p) => p.id === card.dataset.id));
  });
}

export async function renderAgent(host, ctx, id) {
  if (id === ORCHESTRATOR) return renderOrchestrator(host, ctx);
  const plugin = ctx.kernel.plugins.find((p) => p.id === id && p.kind === "agent");
  if (!plugin) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">There is no
      agent <code>${esc(id)}</code> in this project.</div></div>`;
    return;
  }
  const inner = document.createElement("div");
  inner.className = "cfg-page-inner";
  host.innerHTML = "";
  host.appendChild(inner);
  await renderDetail(inner, ctx, plugin, {
    crumb: `<a href="#/config/agents">Agents</a> ▸ ${esc(plugin.title)}`,
    lede: `Spawnable by the orchestrator. Its ceiling is the most it may ever do
      whatever mode the session is in — a subagent has no way to ask you, so a
      request above the ceiling is denied rather than queued.`,
  });
  inner.insertAdjacentHTML("beforeend", workbenchNote(
    `The editable side of this agent is here. What is not yet: the composed
     system prompt it will actually receive, with each section's boundaries
     drawn, and its tool grant as a pattern picker rather than a list.`));
}

function renderOrchestrator(host, ctx) {
  const preset = (ctx.presets?.presets || []).find((p) => p.id === ctx.presets?.active);
  const sections = ctx.prompt?.sections || [];
  const chars = ctx.prompt ? [...ctx.prompt.text].length : 0;
  const tools = preset?.tools || [];
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head" data-kind="agent" data-tier="free">
      <div class="cfg-crumbs"><a href="#/config/agents">Agents</a> ▸ Orchestrator</div>
      <div class="cfg-head-main">
        ${sigilHtml("agent", { big: true })}
        <h2>Orchestrator</h2>
        <code class="cfg-id">@orchestrator</code>
        <span class="cfg-head-badges">${chip("the agent you talk to")}</span>
      </div>
    </header>
    <div class="cfg-lede">The orchestrator is not a definition on disk: it is
      whatever the active composition says a session starts as. Restricting its
      tools states what it does with its own hands — it does not restrict the
      session, because a subagent it spawns still draws from the session's pool.
      To take something away from the whole session, disable the plugin.</div>

    <section class="cfg-sec">
      <h4>Composition</h4>
      <div class="k-facts">
        <a class="k-fact k-link" href="#/config/compositions/${esc(preset?.id || "")}"
          >${esc(preset?.title || ctx.presets?.active || "standard")}</a>
        <span class="k-fact">${tools.length && tools[0] !== "*"
          ? `${tools.length} tool patterns` : "every tool the session has"}</span>
        <span class="k-fact">mode ${esc(preset?.default_mode || "the install default")}</span>
      </div>
    </section>

    <section class="cfg-sec">
      <h4>System prompt</h4>
      <p class="cfg-note">${ctx.prompt
        ? `${sections.length} sections · ${chars.toLocaleString()} characters, as
           composed for the next session.
           <a class="k-link" href="#/config/parts/prompt">Read it section by section →</a>`
        : `The composed prompt could not be read.`}</p>
    </section>

    ${workbenchNote(`The orchestrator's own page is the one that most wants the
      three-column workbench: its instructions beside the exact prompt bytes,
      with the changed range marked as you type.`)}
  </div>`;
}
