// Help ▸ The plugin model.
//
// The page that makes Settings make sense. Settings is dense because it shows
// the whole install — that is a commitment, not an oversight — and this is
// where the commitment is stated and defended.
//
// The three tiers and the "locked is not hidden" rule are the load-bearing
// ideas. Everything else on this page is inventory.

import { esc } from "../util.js";
import { KINDS, PARTS, kindSigil, sigilHtml } from "../config/kinds.js";
import { tierBadge } from "../settings/ui.js";
import { getFacts } from "./view.js";
import { honesty, link, note, pageHtml, quote, sub } from "./ui.js";

// The nine declared kinds, with the sentence each one needs. The labels and
// sigils come from js/config/kinds.js so the two surfaces cannot drift; only
// the explanation is written here.
const KIND_NOTES = {
  tool: "Something the model can call. It declares its own JSON schema, whether "
      + "it is read-only, and the shape it wants to be gated by.",
  prompt_section: "A block of the system prompt. It has an order number, and the "
      + "composed prompt is those blocks in that order.",
  provider: "A model backend — the wire-protocol adapter that turns a request "
          + "into whatever the endpoint expects.",
  agent: "A subagent definition: which tools it gets, which model, how many "
       + "turns it may take, and the permission ceiling it cannot exceed.",
  mcp_server: "An external process speaking the Model Context Protocol. Its "
            + "tools join the pool as ordinary tools once it is connected.",
  policy: "A rule set the runtime consults — the permission engine, the "
        + "tool-call protocol, the update check.",
  hook: "A callback on the loop's lifecycle: which tools are visible for a "
      + "request, whether a call is answered without running the tool, and what "
      + "happens after one runs.",
  panel: "A surface in this web interface.",
  storage: "How a session is written to disk, and in what shape.",
};

const TIER_NOTES = [
  ["free", "Change it, nothing asks.",
    "The value is a form field and saving it is the whole interaction."],
  ["confirm", "Changeable, but it moves agent behaviour in ways that can break things.",
    "Saving goes through a dialog that names the specific risk — the setting "
    + "carries its own sentence about what goes wrong, so the dialog never has "
    + "to ask “are you sure?” about nothing in particular."],
  ["locked", "Not changeable, ever.",
    "The value is rendered as a legible, selectable fact rather than a greyed-out "
    + "input, under a heading that says why it is fixed, and the block always "
    + "ends in something you can actually do instead."],
];

function tierTable() {
  return `<div class="hp-widget">
    ${TIER_NOTES.map(([tier, what, how]) => `
      <div class="hp-tier-row">
        <div class="hp-tier-badge">${tierBadge(tier)}</div>
        <div>
          <div class="hp-tier-what">${esc(what)}</div>
          <div class="hp-tier-how">${esc(how)}</div>
        </div>
      </div>`).join("")}
  </div>`;
}

function kindsHtml(kernel) {
  const count = (kind) => kernel
    ? kernel.plugins.filter((p) => p.kind === kind).length : null;
  return `<div class="hp-kinds">
    ${Object.keys(KINDS).map((kind) => {
      const n = count(kind);
      return `<div class="hp-kind" data-kind="${esc(kind)}">
        <div class="hp-kind-head">
          ${sigilHtml(kind)}
          <span class="hp-kind-name">${esc(KINDS[kind].label)}</span>
          ${n != null ? `<span class="cfg-count">${esc(String(n))}</span>` : ""}
          <a class="hp-node-go" href="#/config/parts/${esc(KINDS[kind].part)}"
            >Settings →</a>
        </div>
        <p class="hp-kind-note">${esc(KIND_NOTES[kind] || "")}</p>
      </div>`;
    }).join("")}
  </div>`;
}

function partsHtml() {
  return `<ul class="hp-list">
    ${PARTS.map((p) => `<li class="hp-li">
      <a class="hp-a" href="#/config/parts/${esc(p.slug)}"><strong>${esc(p.title)}</strong></a>
      — ${esc(p.kinds.map((k) => KINDS[k]?.label || k).join(", "))}
      <span class="hp-dim-inline">(${esc(p.kinds.map(kindSigil).join(" "))})</span>
    </li>`).join("")}
  </ul>`;
}

export async function renderPlugins(host) {
  const body = `
    ${sub("A plugin is not an add-on here")}
    <p class="hp-p">In most apps “plugin” means the optional extras bolted onto a
      core. Here it means <strong>everything</strong>. The shell tool is a plugin.
      The paragraph of the system prompt that tells the model to be concise is a
      plugin. So is the permission policy, the session log format, and the
      connection to your model provider.</p>
    ${quote("The list the Settings UI shows is the list the runtime uses; if "
      + "those two ever diverge the feature is a lie, so they read from one "
      + "registry.", "quickcode/kernel/spec.py")}
    <p class="hp-p">That is why Settings is dense: it is not a summary of the
      install, it <em>is</em> the install. Nothing has been left out to make the
      page shorter.</p>

    ${sub("The kinds")}
    <p class="hp-p">Nine kinds are declared. They are grouped into five pages in
      Settings, because five is how many questions people actually arrive
      with.</p>
    <div id="hp-kinds-slot"><div class="hp-loading">Reading the registry…</div></div>

    ${sub("…and the five pages they land on")}
    ${partsHtml()}

    ${sub("Where a plugin comes from")}
    <dl class="hp-defs">
      <dt class="hp-dt">internal</dt>
      <dd class="hp-dd">Shipped with QuickCode, declared in
        <code>kernel/manifest.py</code>. Same shape as any other — there is no
        privileged side door for the built-ins.</dd>
      <dt class="hp-dt">entrypoint</dt>
      <dd class="hp-dd">Third-party, discovered through Python entry points.</dd>
      <dt class="hp-dt">config</dt>
      <dd class="hp-dd">Data-driven — an MCP server declared in a settings
        file becomes a plugin without anyone writing code.</dd>
      <dt class="hp-dt">authored</dt>
      <dd class="hp-dd">A markdown file you wrote, under
        <code>.quickcode/plugins/</code>. These are the ones with an
        <em>Edit file</em> button, because they are files you own.</dd>
    </dl>

    ${sub("The three tiers")}
    <p class="hp-p">Mutability is declared <strong>per setting</strong>, not per
      plugin. One plugin can hold a knob you may turn freely, a knob that asks
      first, and a fact that is fixed by design — and the card badges itself with
      the strictest of them so a glance is not misleading.</p>
    ${tierTable()}

    ${sub("Why locked never means hidden")}
    <p class="hp-p">This is the commitment the whole surface rests on, so it is
      worth stating plainly.</p>
    ${quote("“locked” means “you cannot edit this”. It never means “you cannot "
      + "see it”: every plugin exposes a view of its raw truth at every tier.",
      "quickcode/kernel/spec.py")}
    <p class="hp-p">In practice that means a fixed setting is shown to you in
      full — the actual value, in a form you can select and copy — under a
      <em>Fixed by design</em> heading that names the invariant being defended.
      Not the policy (“this is dangerous”) but the mechanism (“the trajectory
      replays by sequence number, so the record shape is fixed”). And the block
      never ends in a shrug: there is always a real next action, whether that is
      duplicating the plugin into an editable copy of your own, changing the knob
      that actually governs this one, or reading the contract in full.</p>
    <p class="hp-p">The ${link("#/config/machine-room", "Machine room")} in
      Settings is the filter over everything in that state. It is a view, not a
      location — each of those plugins still has exactly one page, wherever it
      lives.</p>

    ${note("The one thing tiers do not do", `
      <p class="hp-p">A tier is about <em>editing</em>, never about
        <em>switching on</em>. A tool whose only declared setting is the fixed
        fact “I am read-only” is not a locked plugin: nothing was taken away from
        you, the class simply declares what it is. It badges
        ${tierBadge("free")}, because its one real affordance — the enable
        toggle — is free.</p>`)}
  `;

  host.innerHTML = pageHtml("The plugin model", {
    crumb: "Help",
    sigil: "::",
    lede: `Settings shows you a lot because <em>everything</em> in this app is a
      plugin and none of them are hidden from you. This page is the vocabulary:
      what a plugin is here, the nine kinds, where one can come from, and the
      three tiers that decide whether you can change it.`,
    body,
  });

  const facts = await getFacts();
  const slot = host.querySelector("#hp-kinds-slot");
  if (!slot) return;
  slot.innerHTML = kindsHtml(facts?.kernel || null)
    + (facts?.kernel
      ? honesty("live", "The count beside each kind is this install's, read from "
        + "the plugin registry.")
      : honesty("modelled", "The registry could not be read just now, so the "
        + "counts are omitted. The kinds themselves are declared in "
        + "kernel/spec.py and do not vary by install."));
}
