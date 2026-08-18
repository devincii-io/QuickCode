// Compositions — "which agent do new sessions start as".
//
// The kernel calls these presets and keeps the name on disk, on the wire and
// in every session's meta record; the navigation calls them compositions
// because that is the question a person arrives with. Switching one is honest
// about when it applies: a running session keeps the composition it opened
// with, so this changes the *next* one.
//
// The composition editor (agents as a dict of compositions, bindings, per-agent
// prompt bodies) is Phase 6 work. What is here is the truthful read of what
// each one does today plus the one action that already works.

import { esc } from "../util.js";
import { chip, flash, splitError } from "../settings/ui.js";

function patterns(list, empty) {
  if (!list || !list.length) return `<span class="pv-none">${esc(empty)}</span>`;
  if (list.length === 1 && list[0] === "*") {
    return `<span class="pv-all">everything the session has</span>`;
  }
  return list.map((t) => `<code class="pv-tag">${esc(t)}</code>`).join("");
}

function card(p, active, live) {
  const isActive = p.id === active;
  const overrides = Object.keys(p.prompt_overrides || {}).length;
  return `<article class="k-card comp-card${isActive ? " is-active" : ""}"
      data-kind="agent" data-tier="free" data-id="${esc(p.id)}">
    <a class="k-card-main" href="#/config/compositions/${encodeURIComponent(p.id)}">
      <div class="k-card-head">
        <span class="k-sigil" data-kind="agent">@</span>
        <span class="k-title">${esc(p.title)}</span>
        <code class="k-id">${esc(p.id)}</code>
        <span class="k-badges">
          ${chip(p.builtin ? "built-in" : "custom", p.builtin ? "" : "src-config")}
          ${isActive ? `<span class="pv-active">✓ active</span>` : ""}
        </span>
      </div>
      <div class="k-summary">${esc(p.description || "")}</div>
      <dl class="pv-comp">
        <dt>Tools</dt><dd>${patterns(p.tools, "none — nothing callable")}</dd>
        <dt>Subagents</dt><dd>${patterns(p.agents, "none — delegation tools dropped")}</dd>
        <dt>Mode</dt><dd>${p.default_mode
          ? `<code class="pv-tag">${esc(p.default_mode)}</code>`
          : `<span class="pv-none">the install default</span>`}</dd>
        <dt>Prompt</dt><dd>${overrides
          ? `${overrides} section${overrides === 1 ? "" : "s"} rewritten`
          : `<span class="pv-none">unchanged</span>`}</dd>
      </dl>
      ${live ? `<div class="k-facts"><span class="k-fact">${live} running
        session${live === 1 ? "" : "s"} on this one</span></div>` : ""}
    </a>
    <div class="k-card-side">
      ${isActive
        ? `<span class="pv-note">new sessions use this</span>`
        : `<button class="btn" data-use="${esc(p.id)}">Use for new sessions</button>`}
    </div>
  </article>`;
}

export function renderCompositions(host, ctx, selected = "") {
  const data = ctx.presets;
  if (!data) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
      read the compositions.</div></div>`;
    return;
  }
  const live = {};
  for (const id of Object.values(data.live_sessions || {})) live[id] = (live[id] || 0) + 1;
  const list = selected
    ? data.presets.filter((p) => p.id === selected)
    : data.presets;

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">${selected
        ? `<a href="#/config/compositions">Compositions</a> ▸ ${esc(selected)}`
        : "Compositions"}</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="agent">@</span>
        <h2>${esc(selected ? (list[0]?.title || selected) : "Compositions")}</h2>
        <span class="cfg-count">${data.presets.length}</span>
        <span class="cfg-head-actions">
          <a class="btn" href="#/config/new/composition">+ New composition</a>
        </span>
      </div>
    </header>
    <div class="cfg-lede">A composition is the orchestrator's configuration
      under a switchable name: its tools, the agents it may spawn, the prompt
      sections it overrides, the mode it starts in. Sessions keep the one they
      began with — choosing here composes the <em>next</em> session.</div>
    <span class="set-flash" data-flash></span>
    <div class="k-list">${list.map((p) => card(p, data.active, live[p.id] || 0)).join("")
      || `<div class="set-empty">No composition with that id.</div>`}</div>
    ${selected ? `<section class="cfg-soon">
      <h4>Editing — next pass</h4>
      <p>Editing a composition in place (its tool patterns, which agents it may
        spawn, per-agent prompt bodies and bindings) is the authoring pass.
        Today a composition is written in <code>.quickcode/settings.json</code>
        under <code>presets</code>, and everything above is read straight from
        it.</p>
    </section>` : ""}
  </div>`;

  host.querySelector(".k-list").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-use]");
    if (!btn) return;
    e.preventDefault();
    const msg = host.querySelector("[data-flash]");
    try {
      const res = await ctx.api.setPreset(btn.dataset.use);
      data.active = res.active;
      renderCompositions(host, ctx, selected);
      flash(host.querySelector("[data-flash]"),
        `Now “${res.active}” — applies to ${res.applies_to || "new sessions"}.`);
      ctx.railDirty?.();
    } catch (err) {
      flash(msg, splitError(err).detail, "err");
    }
  });
}
