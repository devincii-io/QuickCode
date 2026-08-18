// The rail: the same five sections on every page, so "where am I" is one
// glance, and every destination is a real link with a real URL.
//
// The order is the order of the questions people arrive with —
//   make this agent behave differently        → Agents
//   which agent do new sessions start as      → Compositions
//   what can it do at all / add a capability  → Parts
//   why does it do that, what is fixed        → Machine room
//   where do the models come from             → Install
// — and deliberately not the kernel's `spec.group` order, which groups by
// implementation neighbourhood.

import { esc } from "../util.js";
import { PARTS } from "./kinds.js";
import { ORCHESTRATOR, agentPlugins } from "./agents.js";
import { lockedPlugins } from "./machineroom.js";

function item(href, label, { count = null, active = false, sigil = "", add = false } = {}) {
  return `<a class="rail-item${active ? " active" : ""}${add ? " rail-add" : ""}"
      href="${href}">
    ${sigil ? `<span class="rail-sigil">${esc(sigil)}</span>` : ""}
    <span class="rail-label">${esc(label)}</span>
    ${count != null ? `<span class="rail-count">${count}</span>` : ""}
  </a>`;
}

export function renderRail(node, ctx, route) {
  const at = (...parts) => route.path.join("/") === parts.join("/");
  const startsAt = (head) => route.path[0] === head;
  const agents = agentPlugins(ctx.kernel);
  const presets = ctx.presets?.presets || [];
  const counts = Object.fromEntries(PARTS.map((p) => [
    p.slug, ctx.kernel.plugins.filter((x) => p.kinds.includes(x.kind)).length,
  ]));

  node.innerHTML = `
    <section class="rail-sec">
      <h3><a href="#/config/agents">Agents</a></h3>
      ${item(`#/config/agents/${encodeURIComponent(ORCHESTRATOR)}`, "Orchestrator",
        { sigil: "@", active: at("agents", ORCHESTRATOR) })}
      ${agents.map((p) => item(`#/config/agents/${encodeURIComponent(p.id)}`, p.title,
        { sigil: "@", active: at("agents", p.id) })).join("")}
      ${item("#/config/new/agent", "New agent", { add: true, active: at("new", "agent") })}
    </section>

    <section class="rail-sec">
      <h3><a href="#/config/compositions">Compositions</a></h3>
      ${presets.map((p) => `<a class="rail-item${
          at("compositions", p.id) ? " active" : ""}"
          href="#/config/compositions/${encodeURIComponent(p.id)}">
          <span class="rail-label">${esc(p.title)}</span>
          ${p.id === ctx.presets.active ? `<span class="rail-tick">✓ active</span>` : ""}
        </a>`).join("")}
      ${item("#/config/new/composition", "New composition",
        { add: true, active: at("new", "composition") })}
    </section>

    <section class="rail-sec">
      <h3><a href="#/config/parts/tools">Parts</a></h3>
      ${PARTS.map((p) => item(`#/config/parts/${p.slug}`, p.title, {
        sigil: p.sigil, count: counts[p.slug],
        active: startsAt("parts") && route.path[1] === p.slug,
      })).join("")}
    </section>

    <section class="rail-sec">
      ${item("#/config/machine-room", "Machine room", {
        sigil: "§", count: lockedPlugins(ctx.kernel).length,
        active: startsAt("machine-room"),
      })}
      ${item("#/config/install", "Install", { sigil: "»", active: startsAt("install") })}
    </section>`;
}
