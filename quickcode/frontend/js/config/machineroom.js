// The Machine room: a filter, not a location.
//
// It is the view over every plugin whose tier is `locked`, plus an index of
// the locked *settings* that sit on plugins living elsewhere. A plugin keeps
// exactly one canonical page whatever room links to it — `runtime.subagents`
// belongs to Parts ▸ Policies & limits and is linked from here, never
// duplicated into it.
//
// Why the room exists at all: mixed into a list of editable things, locked
// plugins read as broken. Given their own room with a lede that says "this is
// the contract everything else depends on, here it is in full", they read as
// documentation — which is what they are.

import { esc } from "../util.js";
import { chip, tierBadge } from "../settings/ui.js";
import { summaryOf } from "./explain.js";
import { bodyHtml, canonicalHref, kindLabel, sigilHtml } from "./kinds.js";

// A plugin is "in" the machine room when it is locked *and* it is one of the
// runtime internals — the loop's own machinery. `PluginSpec.tier()` returns
// the strictest tier among a plugin's *knobs*, skipping settings flagged
// `fact`, so a tool whose only setting is the declared `read_only` flag comes
// back `free` and no longer arrives here claiming to be machinery. The kind
// filter below is therefore no longer load-bearing against tools — it still
// holds the room to the four runtime internals whatever else locks itself.
// A tool's `read_only` keeps its own locked tier and is indexed below, on the
// page the tool already has.
const MACHINE_KINDS = ["policy", "hook", "storage", "panel"];

export function lockedPlugins(kernel) {
  return kernel.plugins.filter(
    (p) => p.tier === "locked" && MACHINE_KINDS.includes(p.kind));
}

/** Locked settings on plugins that live somewhere else. They keep their one
 *  canonical page and are only indexed here. */
function lockedElsewhere(kernel) {
  const machine = new Set(lockedPlugins(kernel).map((p) => p.id));
  const out = [];
  for (const p of kernel.plugins) {
    if (machine.has(p.id)) continue;
    for (const s of p.settings || []) {
      if (s.tier === "locked") out.push({ plugin: p, setting: s });
    }
  }
  return out;
}

export function renderMachineRoom(host, ctx) {
  const locked = lockedPlugins(ctx.kernel);
  const elsewhere = lockedElsewhere(ctx.kernel);

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">Machine room</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="policy">§</span>
        <h2>Machine room</h2>
        <span class="cfg-count">${locked.length}</span>
      </div>
    </header>
    <div class="cfg-lede">This is the contract everything else depends on: the
      tool-call handshake, the session log's record shape, the plan-mode gate,
      the subagent report sanitizer. Nothing here is a knob — and nothing here
      is hidden either. Every value is readable in full, and every page ends in
      something you <em>can</em> do.</div>

    <div class="k-list">${locked.map((p) => `
      <article class="k-card" data-kind="${esc(p.kind)}" data-tier="locked">
        <a class="k-card-main" href="${canonicalHref(p)}">
          <div class="k-card-head">
            ${sigilHtml(p.kind)}
            <span class="k-title">${esc(p.title)}</span>
            <code class="k-id">${esc(p.id)}</code>
            <span class="k-badges">${chip(kindLabel(p.kind), "k-" + p.kind)}${tierBadge(p.tier)}</span>
          </div>
          <div class="k-summary">${esc(summaryOf(p))}</div>
          ${bodyHtml(p, ctx.facts)}
        </a>
      </article>`).join("") || `<div class="set-empty">Nothing in this install is
        fully locked, which would be surprising — the tool protocol and the
        session log normally are.</div>`}
    </div>

    ${elsewhere.length ? `
      <h3 class="cfg-sub">Fixed settings on editable plugins<span class="cfg-count"
        >${elsewhere.length}</span></h3>
      <div class="cfg-lede small">These are locked, but their plugin is not: it
        has knobs you can turn as well. Each one is shown on its own page, which
        is the one place it lives.</div>
      <div class="mr-index">${elsewhere.map(({ plugin, setting }) => `
        <a class="mr-row" href="${canonicalHref(plugin)}">
          ${sigilHtml(plugin.kind)}
          <span class="mr-key mono">${esc(setting.key)}</span>
          <span class="mr-title">${esc(setting.title || setting.key)}</span>
          <span class="mr-owner">${esc(plugin.title)}</span>
          ${tierBadge("locked")}
        </a>`).join("")}</div>` : ""}
  </div>`;
}
