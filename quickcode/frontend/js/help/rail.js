// The Help rail: the same seven destinations on every page.
//
// Deliberately the same markup and the same classes as the configuration rail
// (js/config/rail.js, css/config.css:69-99) — .rail-item, .rail-sigil,
// .rail-label — so the two surfaces are visibly one app. What differs is only
// the order, and the order is the order of the questions people arrive with:
//
//   what actually happens when I send a message  → The big picture
//   what are all these things in Settings        → The plugin model
//   how do I read one of those cards             → The six questions
//   what is it allowed to do to my machine       → Permissions & trust
//   just tell me what to do first                → Your first session
//   let me try it                                → Hands-on
//   what were the shortcuts again                → Keyboard & commands

import { esc } from "../util.js";
import { SECTIONS } from "./view.js";

export function renderRail(node, route) {
  if (!node) return;
  const here = route.path[0] || "overview";
  node.innerHTML = `
    <section class="rail-sec">
      <h3>Help</h3>
      ${SECTIONS.map((s) => `
        <a class="rail-item${s.slug === here ? " active" : ""}"
           href="#/help/${esc(s.slug)}"
           ${s.slug === here ? 'aria-current="page"' : ""}
           title="${esc(s.blurb)}">
          <span class="rail-sigil">${esc(s.sigil)}</span>
          <span class="rail-label">${esc(s.title)}</span>
        </a>`).join("")}
    </section>

    <section class="rail-sec">
      <h3>Elsewhere</h3>
      <a class="rail-item" href="#/config/agents">
        <span class="rail-sigil">⚙</span>
        <span class="rail-label">Configuration</span>
      </a>
      <a class="rail-item" href="#/config/machine-room">
        <span class="rail-sigil">§</span>
        <span class="rail-label">Machine room</span>
      </a>
      <a class="rail-item" href="#/config/problems">
        <span class="rail-sigil">!</span>
        <span class="rail-label">Problems</span>
      </a>
    </section>`;
}
