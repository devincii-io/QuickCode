// Help ▸ The six questions.
//
// Every plugin and every setting in Settings answers the same six questions in
// the same order. Once a reader has understood one card they can read all of
// them, which is the only reason a surface that dense is navigable at all. This
// page teaches the pattern once.
//
// The worked example is rendered with `explainHtml` — the *same* function the
// configuration view calls — against a plugin pulled live from this install's
// registry. Rewriting the block here to look like the real one would be a copy
// that drifts, and a help page that teaches an obsolete layout is worse than
// no example.

import { esc } from "../util.js";
import { explainHtml } from "../config/explain.js";
import { canonicalHref, sigilHtml } from "../config/kinds.js";
import { tierBadge } from "../settings/ui.js";
import { getFacts } from "./view.js";
import { honesty, link, note, pageHtml, quote, sub } from "./ui.js";

const QUESTIONS = [
  ["WHAT", "One sentence of plain language. Not the title again, and not the id — "
    + "what this thing is, for someone who has never heard of it.", "always"],
  ["AFFECTS", "Which surfaces it moves, as chips: <b>prompt</b>, <b>tool list</b>, "
    + "<b>loop</b>, <b>storage</b>, <b>ui</b>, <b>permissions</b>, <b>models</b>. "
    + "This is the row to read when you are hunting for the thing that changed "
    + "some behaviour you noticed.", "always"],
  ["WHO", "How far it reaches: the orchestrator only, the agents it is attached "
    + "to, every agent at every depth, or the whole install. A setting that says "
    + "<em>every agent</em> is one you should think about twice.", "always"],
  ["IF CHANGED", "What actually becomes different. Deliberately neutral at every "
    + "tier — it describes the new behaviour, it does not warn you. On a "
    + "<b>confirm</b> setting there is a second, separate sentence that does "
    + "warn you, and it appears in the confirmation dialog rather than on the "
    + "card.", "always"],
  ["WHY FIXED", "The engineering reason this is not a knob. It names the "
    + "invariant, not the policy — “the trajectory replays by sequence number, "
    + "so the record shape is fixed”, never “this is dangerous”.",
    "locked settings only"],
  ["INSTEAD", "The way forward, as a real button. Duplicate this into an "
    + "editable copy, change the knob that actually governs this one, or read "
    + "the contract in full. A fixed setting is never allowed to be a dead "
    + "end.", "locked settings only"],
];

/** The example is chosen from the live registry rather than named here, so a
 *  rename or a removal degrades to a different real plugin instead of to a
 *  page describing one that no longer exists. */
function pickExample(kernel, { locked }) {
  const wants = (p) => (locked
    ? p.tier === "locked" && p.locked_because && p.recourse
    : p.tier !== "locked" && p.summary && p.consequence);
  const preferred = locked
    ? ["runtime.session_log", "runtime.tool_protocol", "hook.plan_mode"]
    : ["runtime.agent_loop", "runtime.compaction", "runtime.permissions"];
  for (const id of preferred) {
    const hit = kernel.plugins.find((p) => p.id === id && wants(p));
    if (hit) return hit;
  }
  return kernel.plugins.find(wants) || null;
}

function exampleHtml(plugin, caption) {
  return `<div class="hp-example">
    <div class="hp-example-head">
      <span class="hp-example-what">${esc(caption)}</span>
      ${sigilHtml(plugin.kind)}
      <strong>${esc(plugin.title)}</strong>
      <code>${esc(plugin.id)}</code>
      ${tierBadge(plugin.tier)}
    </div>
    ${explainHtml(plugin)}
    ${plugin.recourse?.label ? `<div class="hp-example-foot">
      <span class="hp-q-key">Instead</span>
      <span>On its own page this is a button: <em>${esc(plugin.recourse.label)}</em>.</span>
    </div>` : ""}
    <div class="hp-example-foot">
      <a class="hp-a" href="${esc(canonicalHref(plugin))}">Open this card in
        Settings →</a>
    </div>
  </div>`;
}

export async function renderQuestions(host) {
  const body = `
    ${sub("The pattern")}
    <div class="hp-widget">
      ${QUESTIONS.map(([key, text, when]) => `
        <div class="hp-q">
          <div class="hp-q-key">${esc(key)}</div>
          <div class="hp-q-body">${text}
            <span class="hp-q-when">Present: ${esc(when)}.</span></div>
        </div>`).join("")}
    </div>

    ${quote("Every plugin and every setting also answers the same six questions, "
      + "in the same order, wherever it is rendered. Consistency is the point: "
      + "once a reader has understood one card they can read all of them without "
      + "looking twice.", "quickcode/kernel/spec.py")}

    ${sub("A real card, from this install")}
    <p class="hp-p">Both blocks below are rendered by the same code that renders
      them in Settings, against plugins actually loaded in this session. If they
      look slightly different from what this page describes, believe the
      blocks.</p>
    <div id="hp-example-free"><div class="hp-loading">Reading a plugin…</div></div>
    <div id="hp-example-locked"></div>

    ${sub("Two sentences that are not the same sentence")}
    <p class="hp-p">The one thing worth slowing down on. <strong>IF CHANGED</strong>
      and the risk warning look similar and are deliberately different
      things.</p>
    <dl class="hp-defs">
      <dt class="hp-dt">consequence</dt>
      <dd class="hp-dd">Neutral, present at every tier. Says what
        <em>becomes different</em>. “When it runs, the older part of the
        conversation is replaced by a summary.”</dd>
      <dt class="hp-dt">risk</dt>
      <dd class="hp-dd">Exists only on <b>confirm</b> settings. Says what
        <em>goes wrong</em>. “Raising this lets a confused agent burn tokens for
        much longer before it gives up.” You meet it in the confirmation dialog,
        which is why that dialog never has to ask “are you sure?” about nothing
        in particular.</dd>
    </dl>
    <p class="hp-p">So: a <b>free</b> setting shows the consequence alone. A
      <b>confirm</b> setting shows both. A <b>locked</b> setting shows the
      consequence plus the fixed-by-design pair.</p>

    ${note("When a row is dotted or dashed", `
      <p class="hp-p">Some plugins have not had all six written yet. Rather than
        leave a blank, the card infers <strong>AFFECTS</strong> and
        <strong>WHO</strong> from the plugin's kind and marks the inferred value
        with a dotted underline or a dashed chip — hover it and it says so. A
        consequence is never inferred: if nobody wrote one, the row is simply not
        there. Guessing what a setting does would be the one failure this whole
        layer exists to prevent.</p>`)}

    <p class="hp-p">${link("#/help/permissions", "Permissions & trust")} is the
      best place to practise reading these, because it is where the locked tier
      earns its keep.</p>
  `;

  host.innerHTML = pageHtml("The six questions", {
    crumb: "Help",
    sigil: "¶",
    lede: `Settings has hundreds of rows and exactly one layout. Every plugin and
      every setting answers the same six questions in the same order — so
      learning to read one card is learning to read all of them.`,
    body,
  });

  const facts = await getFacts();
  const freeSlot = host.querySelector("#hp-example-free");
  const lockedSlot = host.querySelector("#hp-example-locked");
  if (!freeSlot) return;

  if (!facts?.kernel) {
    freeSlot.innerHTML = `<div class="hp-degraded">The plugin registry could not
      be read just now, so no example is shown. Inventing one would defeat the
      point of the example.</div>`;
    return;
  }

  const free = pickExample(facts.kernel, { locked: false });
  const locked = pickExample(facts.kernel, { locked: true });
  freeSlot.innerHTML = free
    ? exampleHtml(free, "An editable plugin")
    : `<div class="hp-degraded">No editable plugin in this install has written
        its explanation yet.</div>`;
  if (lockedSlot) {
    lockedSlot.innerHTML = (locked
      ? exampleHtml(locked, "A fixed-by-design plugin")
      : `<div class="hp-degraded">Nothing in this install is fully locked, which
          would be surprising — the tool protocol and the session log normally
          are.</div>`)
      + honesty("live", "Both blocks are the live plugin records, rendered by "
        + "js/config/explain.js — the same function Settings calls.");
  }
}
