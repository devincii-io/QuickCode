// USED BY — "if I change this, what moves".
//
// Every other block on a detail page describes the plugin. This one describes
// its blast radius, and it is the question the whole configuration view exists
// to answer: nobody opens `tool.bash` to admire the schema, they open it
// because they are about to take it away from something and want to know what
// that costs.
//
// The rows come straight off the payload — `plugin.used_by`, built server-side
// in kernel/registry.py by resolving each composition's orchestrator once per
// registry build. The browser does not reconstruct it, for the same reason it
// does not reconstruct the composed prompt: a reconstruction drifts, and a
// reconstruction that drifts is worse than none because it is believed.
//
// Self-contained on purpose. No new CSS — the row is the machine-room index
// row (`.mr-*`), which is already the vocabulary for "a compact list of links
// to pages" — and no wiring: every row is a plain `<a href="#/config/…">`, and
// the router (or main.js's hashchange listener, from outside the view) does
// the rest.

import { esc } from "../util.js";
import { sigilHtml } from "./kinds.js";

/** A composition is a named set, not a kind of plugin, so it gets a tile of
 *  its own rather than borrowing the agent hue. An unknown `data-kind` falls
 *  back to `--fg-dim` by design (config.css:190), which is exactly right for
 *  the one row type here that is not a plugin. */
function tileHtml(use) {
  if (use.kind === "agent") return sigilHtml("agent");
  return `<span class="k-sigil" title="composition">{}</span>`;
}

// What "nothing uses this" means is different per kind, and the difference is
// the whole value of the empty state. An unreferenced tool is a finding; an
// unreferenced prompt section is the normal case.
const EMPTY = {
  tool: `No composition grants this tool and no agent definition lists it, so
    nothing in this project can call it. That is usually a typo in a
    composition's tool list rather than a decision.`,
  agent: `No composition lets its orchestrator spawn this agent and no other
    agent may delegate to it, so nothing here can reach it. It is still
    resolvable by name from the agent workbench.`,
  prompt_section: `Nothing re-lists or rewrites this section, which is the
    normal case: a section is in the composed prompt for every agent that does
    not state a section list of its own.`,
  mcp_server: `No composition or agent names this server's tools. They are in
    the session pool if the server is connected — nothing narrows to them.`,
};
const EMPTY_FALLBACK = `No composition or agent names this one. It applies
  wherever it applies: nothing here narrows it, and nothing here overrides it.`;

/** Tools nobody lists are still reachable by every agent that inherits, and
 *  saying so is the difference between an honest list and a misleading one. */
function inheritNote(plugin, ctx) {
  if (plugin.kind !== "tool") return "";
  const inheriting = (ctx?.kernel?.plugins || []).filter(
    (p) => p.kind === "agent" && !p.metadata?.tools);
  if (!inheriting.length) return "";
  const names = inheriting.map((p) => p.title || p.id).join(", ");
  const one = inheriting.length === 1;
  return `<p class="cfg-note">Not listed above: <b>${inheriting.length}</b> agent${
    one ? "" : "s"} (${esc(names)}) state${one ? "s" : ""} no tool list at all and
    inherit${one ? "s" : ""} whatever the agent that spawned ${
    one ? "it" : "them"} holds — so this tool reaches ${one ? "it" : "them"} exactly
    when it reaches ${one ? "its" : "their"} spawner.</p>`;
}

/**
 * The USED BY section for one plugin, as HTML.
 *
 * @param {object} plugin  a kernel plugin payload, carrying `used_by`
 * @param {object} ctx     the config view context (used only for the footnote)
 * @returns {string} a `<section>`, always — the block is on every page, because
 *   "nothing uses this" is an answer and hiding it would make the reader guess
 *   whether the question was even asked.
 */
export function usedByHtml(plugin, ctx = {}) {
  const uses = Array.isArray(plugin?.used_by) ? plugin.used_by : [];
  const compositions = uses.filter((u) => u.kind === "composition").length;
  const agents = uses.length - compositions;

  const lede = uses.length
    ? `${compositions} composition${compositions === 1 ? "" : "s"} and ${
        agents} agent definition${agents === 1 ? "" : "s"} reach this plugin.
       Each row is the page that states it — change it there, not here.`
    : esc(EMPTY[plugin?.kind] || EMPTY_FALLBACK).replace(/\s+/g, " ");

  return `<section class="cfg-sec" data-usedby>
    <h4>Used by${uses.length ? ` <span class="cfg-count">${uses.length}</span>` : ""}</h4>
    <p class="cfg-note">${lede}</p>
    ${uses.length ? `<div class="mr-index">${uses.map((u) => `
      <a class="mr-row" href="${esc(u.href)}"
         title="${esc(u.title)} — ${esc(u.via)}">
        ${tileHtml(u)}
        <span class="mr-key mono">${esc(u.id)}</span>
        <span class="mr-title">${esc(u.via)}</span>
        <span class="mr-owner">${u.kind === "agent" ? "agent" : "composition"}</span>
      </a>`).join("")}</div>` : ""}
    ${inheritNote(plugin, ctx)}
  </section>`;
}

/** The same block, appended to a host. For callers that build their page by
 *  node rather than by template. */
export function renderUsedBy(host, plugin, ctx = {}) {
  const wrap = document.createElement("div");
  wrap.innerHTML = usedByHtml(plugin, ctx);
  const node = wrap.firstElementChild;
  if (node) host.appendChild(node);
  return node;
}

/** How many things reach a plugin — for a card badge, without building HTML. */
export function usedByCount(plugin) {
  return Array.isArray(plugin?.used_by) ? plugin.used_by.length : 0;
}
