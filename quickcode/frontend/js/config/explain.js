// The explanation layer: the six questions, asked in the same order of every
// plugin and every setting, everywhere they appear.
//
//   WHAT        one line, plain language
//   AFFECTS     prompt · tool list · loop · storage · ui · permissions · models
//   WHO         orchestrator / named agents / every agent / the install
//   IF CHANGED  what actually becomes different, at every tier
//   WHY FIXED   locked only: the reason it is not a knob
//   INSTEAD     locked only: the recourse, as a real button
//
// The backend fields this reads (`summary`, `affects`, `audience`,
// `consequence`, `locked_because`, `recourse`) are on kernel/spec.py, but each
// one defaults to empty, so a plugin may simply not have written it. Every
// field degrades to something true rather than to a blank: the summary falls
// back to the first sentence of `description`, and AFFECTS/WHO are inferred
// from the plugin's kind and say so on hover. Nothing here invents a
// consequence it was not told.

import { esc } from "../util.js";
import { duplicateRefusal } from "./kinds.js";

const EFFECT_LABEL = {
  prompt: "prompt", tool_list: "tool list", loop: "loop", storage: "storage",
  ui: "ui", permissions: "permissions", models: "models",
};

const AUDIENCE_LABEL = {
  orchestrator: "the orchestrator only",
  named_agents: "the agents it is attached to",
  all_agents: "every agent, at every depth",
  install: "the whole install",
};

const INFERRED = {
  tool:           { affects: ["tool_list"], who: "every agent granted it" },
  prompt_section: { affects: ["prompt"], who: "the orchestrator's prompt" },
  agent:          { affects: ["models", "tool_list"], who: "this agent, whenever it is spawned" },
  mcp_server:     { affects: ["tool_list"], who: "every agent granted its tools" },
  provider:       { affects: ["models"], who: "the whole install" },
  policy:         { affects: ["permissions"], who: "every agent, always" },
  hook:           { affects: ["loop"], who: "every agent, always" },
  storage:        { affects: ["storage"], who: "the whole install" },
  panel:          { affects: ["ui"], who: "the whole install" },
};

const INFER_NOTE = "inferred from the plugin's kind — the kernel does not declare this yet";

/** ≤ 90 characters of plain language. `description` is the paragraph; this is
 *  the line, so falling back to its first sentence is the honest degradation. */
export function summaryOf(plugin) {
  if (plugin.summary) return plugin.summary;
  const text = String(plugin.description || "").trim();
  if (!text) return "";
  const stop = text.search(/\.(\s|$)/);
  return stop > 0 && stop < 120 ? text.slice(0, stop + 1) : text;
}

function affectsOf(plugin) {
  const declared = plugin.affects && plugin.affects.length ? plugin.affects : null;
  return { list: declared || INFERRED[plugin.kind]?.affects || [], inferred: !declared };
}

function whoOf(plugin) {
  if (plugin.audience) return { text: AUDIENCE_LABEL[plugin.audience] || plugin.audience, inferred: false };
  return { text: INFERRED[plugin.kind]?.who || "the whole install", inferred: true };
}

export function affectsChips(plugin) {
  const { list, inferred } = affectsOf(plugin);
  if (!list.length) return "";
  return list.map((e) => `<span class="k-affect${inferred ? " inferred" : ""}"
    ${inferred ? `title="${esc(INFER_NOTE)}"` : ""}>${esc(EFFECT_LABEL[e] || e)}</span>`).join("");
}

/** The six-question block, minus the rows the kernel cannot answer yet.
 *  `omitWhyFixed` is set when a Fixed-by-design block below is about to say the
 *  same sentence per setting — one page, one statement of the reason. */
export function explainHtml(plugin, { omitWhyFixed = false } = {}) {
  const who = whoOf(plugin);
  const rows = [
    ["WHAT", esc(summaryOf(plugin)) || `<span class="k-dim">No summary written yet.</span>`, ""],
    ["AFFECTS", affectsChips(plugin), ""],
    ["WHO", `<span${who.inferred ? ` title="${esc(INFER_NOTE)}" class="inferred-text"` : ""}
      >${esc(who.text)}</span>`, ""],
  ];
  if (plugin.consequence) rows.push(["IF CHANGED", esc(plugin.consequence), ""]);
  if (plugin.description && plugin.description !== summaryOf(plugin)) {
    rows.push(["DETAIL", esc(plugin.description), "detail"]);
  }
  if (plugin.tier === "locked" && !omitWhyFixed) {
    rows.push(["WHY FIXED", esc(lockedBecause(plugin)), ""]);
  }
  return `<dl class="k-explain">${rows.map(([k, v, cls]) =>
    `<dt>${k}</dt><dd class="${cls}">${v}</dd>`).join("")}</dl>`;
}

const KIND_LOCK_REASON = {
  policy: "This is a contract the rest of the app is written against: the "
        + "permission gate and the trajectory both assume it holds for every call.",
  hook: "The loop's lifecycle is what makes a run reconstructable afterwards. "
      + "A hook that could be rewritten would make the record of what happened "
      + "disagree with what happened.",
  storage: "The session log replays by sequence number, so its record shape is "
         + "fixed. Everything that reads a past session depends on it.",
  prompt_section: "This section is composed from the session's own facts rather "
                + "than typed, so there is nothing here to write into.",
  tool: "A tool declares what it is; the declaration is what the permission gate "
      + "and the parallel-read rule are built on.",
};

export function lockedBecause(plugin, s = null) {
  return (s && s.locked_because) || plugin.locked_because
    || KIND_LOCK_REASON[plugin.kind]
    || "This one is part of how QuickCode works. It is fully readable here and "
     + "never editable.";
}

/** "Fixed by design": the locked-tier presentation.
 *
 *  Three rules, all of them visible in the markup: neutral (never the danger
 *  colour — locked is structure, not an error), the value legible and
 *  selectable rather than a disabled input, and a real recourse button at the
 *  end so it is documentation with an exit rather than a dead end. */
export function fixedBlockHtml(plugin, settings) {
  if (!settings.length) return "";
  const value = (s) => {
    if (s.type === "bool") return `<b class="k-lit">${s.value ? "true" : "false"}</b>`;
    if (s.type === "text" || s.type === "list") {
      const text = Array.isArray(s.value) ? s.value.join("\n") : String(s.value ?? "");
      return `<pre class="raw k-fixed-body">${esc(text)}</pre>`;
    }
    return `<b class="k-lit">${esc(String(s.value ?? ""))}</b>`;
  };
  return `<section class="k-fixed-block">
    <h4>Fixed by design</h4>
    ${settings.map((s) => `<div class="k-fixed-row">
      <div class="k-fixed-head">
        <span class="k-fixed-title">${esc(s.title || s.key)}</span>
        <code class="k-fixed-key">${esc(s.key)}</code>
      </div>
      <div class="k-fixed-value">${value(s)}</div>
      ${s.help ? `<p class="k-fixed-help">${esc(s.help)}</p>` : ""}
      <p class="k-fixed-why"><span class="k-why">Why fixed</span> ${
        esc(lockedBecause(plugin, s))}</p>
    </div>`).join("")}
    ${recourseHtml(plugin)}
  </section>`;
}

/** The duplicate affordance, in the state it is actually in. Three cases, and
 *  the markup states which one it is in `data-dup` so the page that mounts it
 *  only has to attach the click:
 *
 *    edit       an authored plugin is a file you own — open it
 *    duplicate  a built-in that copies — write the editable copy
 *    go / none  a kind that refuses to copy: the recourse where there is one,
 *               and either way the reason, in prose, not in a tooltip
 *
 *  `duplicateRefusal` is the browser-side half of the same table
 *  `kernel/authoring/store.py` enforces, so the button can only ever be here
 *  when the press would succeed. */
function dupHtml(plugin) {
  if (plugin.source === "authored") {
    return `<button class="btn" data-dup="edit"
      title="This plugin is a file you own.">⧉ Edit this file</button>`;
  }
  const refused = duplicateRefusal(plugin);
  if (!refused) {
    return `<button class="btn" data-dup="duplicate"
      title="Writes a copy under .quickcode/plugins/ in which nothing is locked.
             The original is untouched and stays enabled."
      >⧉ Duplicate for an editable copy</button>`;
  }
  // A button that turned into a different button with its explanation hidden
  // in a tooltip is the wordless refusal this layer exists to stop shipping,
  // so the reason is rendered next to it and is readable without hovering.
  return `<button class="btn" ${refused.href
      ? `data-dup="go" data-dup-href="${esc(refused.href)}"`
      : `data-dup="none" disabled`} title="${esc(refused.why)}"
    >${esc(refused.label || "⧉ Duplicate — not for this kind")}</button>
    <p class="dup-why">${esc(refused.why)}</p>`;
}

/** Never a dead end. Whatever is fixed here, the block ends in something you
 *  can do: the plugin's declared `recourse` when the kernel supplies one, the
 *  duplicate affordance in whichever state it is genuinely in, and the raw
 *  definition — which every locked plugin has always let you read. */
export function recourseHtml(plugin) {
  const r = plugin.recourse;
  return `<div class="k-recourse">
    <span class="k-recourse-lead">Instead</span>
    ${r ? `<button class="btn" data-recourse="${esc(r.action)}"
             data-target="${esc(r.target || "")}">${esc(r.label)}</button>` : ""}
    ${dupHtml(plugin)}
    <button class="btn" data-raw>Read the full definition →</button>
  </div>`;
}
