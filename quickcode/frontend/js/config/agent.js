// The agent workbench: one page per agent, everything about that agent, and a
// live preview of the exact bytes it will receive.
//
// This page exists because an agent's configuration used to be scattered across
// four surfaces — its settings on one page, its instructions behind a View
// button, its tool grant as three uneditable `<code>` tags, its composed prompt
// nowhere at all — so "what does explore actually get" had no answer anywhere.
//
// Three properties it has to keep:
//
// * **Nothing is reconstructed.** Every value, the prompt bytes and the tool
//   schemas come from `/resolved` and `/preview`, which run the same resolver
//   and the same prompt code the runner runs.
// * **Provenance on every value.** "Why is this ask when the default is yolo"
//   is answered in place, from `Resolved.chain`, not by reading three files.
// * **Absences are shown.** A denied tool is listed with the reason it is
//   denied and a missing prompt section with the reason it is missing. An
//   omitted key answers nothing.

import { esc } from "../util.js";
import { chip, flash, openPluginView, splitError, tierBadge } from "../settings/ui.js";
import { renderSettingsForm } from "../settings/fields.js";
import { sigilHtml } from "./kinds.js";
import { mountPreview } from "./preview.js";
import { openToolPicker } from "./toolpicker.js";
import { duplicatePlugin } from "./create/scaffold.js";

export const ORCHESTRATOR = "@orchestrator";

const num = (n) => Number(n || 0).toLocaleString();

// ---- provenance -----------------------------------------------------------

const LAYER_LABEL = {
  default: "the built-in default",
  user: "your user settings",
  project: "this project's settings",
  preset: "the composition",
  agent: "the agent definition",
  session: "this session",
  call: "the spawn call",
  parent: "the spawning agent",
  runtime: "the runtime",
};

/** One provenance entry as a hoverable "← where this came from" cell. */
function provHtml(p, { label = "" } = {}) {
  if (!p || !p.layer) return `<span class="wb-prov none">—</span>`;
  const where = LAYER_LABEL[p.layer] || p.layer;
  const detail = [where, p.source, p.rule && `rule: ${p.rule}`, p.path, p.note]
    .filter(Boolean).join(" · ");
  return `<span class="wb-prov" data-layer="${esc(p.layer)}"
    title="${esc(detail)}">← ${esc(label || p.layer)}</span>`;
}

function chainStrip(chain, key) {
  const entries = (chain || {})[key] || [];
  if (!entries.length) return "";
  return `<span class="wb-chain">${entries.map((p, i) => `
    <span class="wb-slot${i === entries.length - 1 ? " win" : ""}"
          title="${esc([LAYER_LABEL[p.layer] || p.layer, p.source, p.rule, p.note]
            .filter(Boolean).join(" · "))}">${esc(p.layer)}${
      p.rule ? ` ${esc(p.rule)}` : ""}</span>`).join('<span class="wb-arrow">→</span>')}
  </span>`;
}

// ---- the sections ---------------------------------------------------------

function identityHtml(d) {
  const badge = d.builtin ? chip("built-in") : chip("authored", "src-config");
  return `<section class="wb-sec" data-sec="identity">
    <h4>Identity ${badge}</h4>
    <dl class="wb-rows">
      <dt>name</dt><dd><code>${esc(d.id)}</code>
        ${d.builtin ? `<span class="wb-note">fixed — built-in agents keep their
          name so a composition naming one keeps meaning it</span>` : ""}</dd>
      <dt>role</dt><dd>${esc(d.role)}
        <span class="wb-note">${d.role === "orchestrator"
          ? "the agent you talk to: it can ask you for permission and it can plan"
          : "spawnable, headless: it can never ask you for permission, so its "
            + "ceiling is the whole of its answer"}</span></dd>
      <dt>summary</dt><dd>${esc(d.description || "—")}</dd>
      <dt>defined in</dt><dd><code class="wb-src">${esc(d.path || d.source)}</code>
        ${d.path ? `<span class="wb-note">a file you own</span>` : ""}</dd>
    </dl>
  </section>`;
}

function instructionsHtml(d) {
  const isOrchestrator = d.id === ORCHESTRATOR;
  const lands = isOrchestrator
    ? `The orchestrator's instructions are the prompt <em>sections</em>, composed
       in order. Editing one is a per-section job and lives on
       <a class="k-link" href="#/config/parts/prompt">Parts ▸ Prompt</a>; what is
       here is the composed result, with every section's boundary drawn.`
    : `Lands at <code>&lt;role&gt;</code> — between the identity block and the
       environment block of <code>prompts/subagent.py</code>. Until you know
       where it lands, editing the body is editing something whose position you
       cannot see.`;
  return `<section class="wb-sec" data-sec="instructions">
    <h4>Instructions</h4>
    ${isOrchestrator ? `<p class="wb-note block">${lands}</p>` : `
      <textarea class="wb-body" spellcheck="false"
        placeholder="This agent has no instructions of its own."
        >${esc(d.prompt_body || "")}</textarea>
      <div class="wb-body-foot">
        <span class="wb-note">${lands}</span>
        <span class="wb-body-count"></span>
      </div>
      <p class="wb-note block">Typing here re-renders the preview from the
        server. Saving the body to disk is the authoring pass — this agent's
        definition is
        <code>${esc(d.path || "built into QuickCode")}</code>${d.path
          ? "" : ", and a built-in definition is duplicated rather than edited"}.</p>`}
  </section>`;
}

function toolsHtml(d) {
  const granted = d.resolved?.tools || [];
  const patterns = d.grant?.patterns || [];
  return `<section class="wb-sec" data-sec="tools">
    <h4>Tools <span class="wb-count">${granted.length} of ${
      (d.pool || []).length}</span></h4>
    <div class="wb-grant">
      ${d.grant?.inherits
        ? `<span class="wb-inherit">everything the spawning agent holds —
             <code>tools: null</code></span>`
        : patterns.map((p) => `<code class="wb-pattern">${esc(p)}</code>`).join("")
          || `<span class="wb-inherit">no patterns</span>`}
      <button class="btn" data-picker>Change…</button>
    </div>
    <div class="wb-footer-line">${esc(d.footer || "")}</div>
    <div class="wb-tool-tags">${granted.map((t) => {
      const p = (d.resolved.chain || {})[`tools.${t}`] || [];
      const last = p[p.length - 1];
      return `<span class="wb-tool" title="${esc(
        [LAYER_LABEL[last?.layer] || last?.layer, last?.source,
         last?.rule && `matched by ${last.rule}`, last?.note]
          .filter(Boolean).join(" · "))}">${esc(t)}</span>`;
    }).join("")}</div>
    ${(d.denied || []).length ? `<details class="wb-denied">
      <summary>${d.denied.length} in the pool this agent does not get — with the
        reason</summary>
      ${d.denied.map((r) => `<div class="wb-denied-row">
        <code>${esc(r.name)}</code><span>${esc(r.reason)}</span></div>`).join("")}
    </details>` : ""}
    ${d.grant?.editable === false ? `<p class="wb-note block">${
      esc(d.grant.editable_reason)}</p>` : ""}
  </section>`;
}

function modelsHtml(d) {
  const m = d.models || {};
  return `<section class="wb-sec" data-sec="models">
    <h4>Models</h4>
    <dl class="wb-rows">
      <dt>default</dt><dd><code>${esc(m.model || "—")}</code>
        ${m.resolved && m.resolved !== m.model
          ? `<span class="wb-note">resolves to <code>${esc(m.resolved)}</code>
             through the active profile</span>` : ""}
        ${provHtml(m.provenance)}</dd>
      <dt>allowed</dt><dd>${m.allowed?.length
        ? m.allowed.map((x) => `<code class="wb-pattern">${esc(x)}</code>`).join("")
        : `<span class="wb-note">any model the provider offers</span>`}
        ${chainStrip(d.resolved?.chain, "models")}</dd>
      <dt>caller may choose</dt><dd>${m.selectable
        ? "yes"
        : `no — pinned to <code>${esc(m.model)}</code>; an override is refused
           rather than ignored`}</dd>
    </dl>
    <div class="wb-form" data-form="models"></div>
  </section>`;
}

function limitsHtml(d) {
  const l = d.limits || {};
  return `<section class="wb-sec" data-sec="limits">
    <h4>Limits &amp; permissions</h4>
    <dl class="wb-rows">
      <dt>ceiling</dt><dd><code>${esc(l.ceiling)}</code>
        ${provHtml(l.ceiling_provenance)}
        ${chainStrip(d.resolved?.chain, "ceiling")}
        <span class="wb-note">${d.role === "orchestrator"
          ? "the most this session may ever be raised to; the mode stays live below it"
          : "the most this agent may ever do, whatever mode the session is in"}</span></dd>
      <dt>max turns</dt><dd>${l.max_turns_applies
        ? `${esc(l.max_turns)} <span class="wb-note">the delegation budget one
             spawned instance gets: one turn for the spawn, one per resume</span>`
        : `<span class="wb-na">${esc(l.max_turns)} — not applicable to the
             orchestrator, which is not spawned and has no delegation budget</span>`}
        ${chainStrip(d.resolved?.chain, "max_turns")}</dd>
      <dt>depth</dt><dd><span class="wb-inherited">inherited: ${esc(l.max_depth)}</span>
        <a class="k-link" href="#/config/parts/policies/runtime.subagents">→ Policies</a>
        <span class="wb-note">decides whether this agent may spawn at all</span></dd>
      <dt>agents</dt><dd><span class="wb-inherited">inherited: ${esc(l.max_agents)}</span>
        <a class="k-link" href="#/config/parts/policies/runtime.subagents">→ Policies</a></dd>
    </dl>
    <div class="wb-form" data-form="limits"></div>
  </section>`;
}

function delegationHtml(d) {
  const spawns = d.spawns || [];
  const denied = d.denied_spawns || [];
  return `<section class="wb-sec" data-sec="delegation">
    <h4>Delegation <span class="wb-count">${spawns.length}</span></h4>
    ${spawns.length
      ? `<div class="wb-tool-tags">${spawns.map((s) => `
          <a class="wb-tool" href="#/config/agents/${encodeURIComponent(s.id)}"
             title="${esc([s.granted_by?.layer, s.granted_by?.rule]
               .filter(Boolean).join(" · "))}">${esc(s.id)}</a>`).join("")}</div>
         <p class="wb-note block">The delegation pair (<code>agent</code>,
           <code>send_message</code>) is granted by depth, never by allowlist —
           this agent has it because it has something to spawn.</p>`
      : `<p class="wb-note block">Cannot spawn. The delegation pair is granted by
           depth and never by pattern, so listing <code>agent</code> in this
           agent's tools would not change that. It has nothing to spawn either
           because its patterns name nothing, or because the depth limit is
           already reached.</p>`}
    ${denied.length ? `<details class="wb-denied">
      <summary>${denied.length} agent${denied.length === 1 ? "" : "s"} it may not
        spawn — with the reason</summary>
      ${denied.map((r) => `<div class="wb-denied-row">
        <code>${esc(r.id)}</code><span>${esc(r.reason)}</span></div>`).join("")}
    </details>` : ""}
  </section>`;
}

function problemsHtml(d) {
  const rows = d.problems || [];
  if (!rows.length) return "";
  return `<section class="wb-sec wb-problems">
    <h4>Problems <span class="wb-count">${rows.length}</span></h4>
    ${rows.map((p) => `<div class="wb-problem" data-sev="${esc(p.severity)}">
      <span class="wb-sev">${esc(p.severity)}</span>
      <div><div class="wb-problem-msg">${esc(p.message)}</div>
        ${p.fix ? `<div class="wb-problem-fix">${esc(p.fix)}</div>` : ""}</div>
      <code class="wb-problem-code">${esc(p.code)}</code>
    </div>`).join("")}
  </section>`;
}

function headHtml(d) {
  const facts = [
    `${(d.resolved?.tools || []).length} tools`,
    `ceiling ${d.limits?.ceiling}`,
    d.models?.model ? `model ${d.models.model}` : "",
    `${(d.spawns || []).length} spawnable`,
  ].filter(Boolean);
  return `<header class="cfg-head" data-kind="agent" data-tier="free">
    <div class="cfg-crumbs"><a href="#/config/agents">Agents</a> ▸ ${esc(d.title)}</div>
    <div class="cfg-head-main">
      ${sigilHtml("agent", { big: true })}
      <h2>${esc(d.title)}</h2>
      <code class="cfg-id">${esc(d.id)}</code>
      <span class="cfg-head-badges">
        ${d.id === ORCHESTRATOR ? chip("you talk to this one") : chip("spawnable")}
        ${d.builtin ? tierBadge("locked", { label: "built-in" }) : chip("yours", "src-config")}
      </span>
      <span class="cfg-head-actions">
        ${d.id === ORCHESTRATOR ? "" : d.builtin
          ? `<button class="ghost-btn" data-dup title="Write an editable markdown
              copy of this agent under .quickcode/plugins/. Every line that is
              fixed here becomes plain text there; this one is untouched and
              stays available.">⧉ Duplicate</button>`
          : `<a class="ghost-btn" href="#/config/edit/${
              encodeURIComponent(`agent.${d.id}`)}">Edit file</a>`}
        ${d.id === ORCHESTRATOR ? "" : `<button class="ghost-btn" data-raw>Raw</button>`}
      </span>
    </div>
    <div class="wb-facts">${facts.map((f) => `<span class="k-fact">${esc(f)}</span>`).join("")}
      <span class="k-fact mono">${esc(d.resolved_against?.preset_title
        || d.resolved_against?.preset || "")}</span>
      ${d.resolved_against?.parent
        ? `<span class="k-fact">under ${esc(d.resolved_against.parent)}</span>` : ""}
      <span class="k-fact mono" title="the digest of this resolution"
        >${esc((d.digest || "").slice(7, 17))}</span>
    </div>
  </header>`;
}

// ---- the page -------------------------------------------------------------

export async function renderWorkbench(host, ctx, id, query = {}) {
  host.innerHTML = `<div class="cfg-page-inner"><div class="set-loading">Resolving
    ${esc(id)}…</div></div>`;

  let data;
  try {
    data = await ctx.api.resolvedAgent(id, {
      preset: query.preset || "", parent: query.parent || "", conv: query.conv || "",
    });
  } catch (err) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
      resolve <code>${esc(id)}</code>: ${esc(splitError(err).detail)}</div></div>`;
    return;
  }

  const plugin = ctx.kernel.plugins.find((p) => p.id === `agent.${id}`);
  const settings = plugin?.settings || [];
  const modelKeys = new Set(["model", "models", "model_selectable"]);

  host.innerHTML = `<div class="wb">
    <div class="wb-editor">
      ${headHtml(data)}
      <span class="set-flash" data-flash></span>
      ${identityHtml(data)}
      ${instructionsHtml(data)}
      ${toolsHtml(data)}
      ${modelsHtml(data)}
      ${limitsHtml(data)}
      ${delegationHtml(data)}
      ${problemsHtml(data)}
    </div>
    <div class="wb-preview-slot"></div>
  </div>`;

  const preview = mountPreview(host.querySelector(".wb-preview-slot"));
  preview.update(data);

  // The tiered settings machinery is reused verbatim: the 403 for a locked
  // setting, the 409 carrying the server's own words as the risk, and the 200.
  // Re-implementing it here would be a second protocol that could disagree.
  if (plugin && settings.length) {
    const put = (slot, keys) => {
      const node = host.querySelector(`[data-form="${slot}"]`);
      const chosen = settings.filter((s) => keys.has(s.key) && s.tier !== "locked");
      if (!node || !chosen.length) return;
      node.appendChild(renderSettingsForm({ ...plugin, settings: chosen }, {
        api: ctx.api,
        onUpdated: async (fresh) => {
          Object.assign(plugin, fresh);
          ctx.touched?.(plugin);
          await refresh();
        },
      }));
    };
    put("models", modelKeys);
    put("limits", new Set(["max_turns", "mode_cap"]));
  }

  async function refresh() {
    try {
      const fresh = await ctx.api.resolvedAgent(id, {
        preset: query.preset || "", parent: query.parent || "",
        conv: query.conv || "",
      });
      Object.assign(data, fresh);
      preview.update(fresh);
    } catch { /* the page still shows the last good answer */ }
  }

  // ---- the live draft -----------------------------------------------------

  const draft = { composition: {}, prompt_body: null };
  let pending = null;

  async function repreview() {
    preview.busy();
    try {
      const body = { parent: query.parent || "", preset: query.preset || "" };
      if (Object.keys(draft.composition).length) body.composition = draft.composition;
      if (draft.prompt_body !== null) body.prompt_body = draft.prompt_body;
      const fresh = await ctx.api.previewAgent(id, body);
      preview.update(fresh);
      return fresh;
    } catch (err) {
      preview.fail(splitError(err).detail);
      return null;
    } finally {
      preview.idle();
    }
  }

  function schedule() {
    clearTimeout(pending);
    pending = setTimeout(repreview, 220);
  }

  const bodyEl = host.querySelector(".wb-body");
  if (bodyEl) {
    const count = host.querySelector(".wb-body-count");
    const paintCount = () => {
      count.textContent = `${num([...bodyEl.value].length)} chars`;
    };
    paintCount();
    bodyEl.addEventListener("input", () => {
      draft.prompt_body = bodyEl.value;
      paintCount();
      schedule();
    });
  }

  host.querySelector("[data-picker]")?.addEventListener("click", () => {
    openToolPicker({
      agent: id,
      data,
      // Every edit re-resolves server-side, so the row states, the footer
      // sentence and the preview all move together and cannot disagree.
      onChange: async ({ patterns, inherits }) => {
        draft.composition = { ...draft.composition, tools: inherits ? null : patterns };
        return await repreview();
      },
      onApply: async ({ patterns, inherits }) => {
        const composition = { tools: inherits ? null : patterns };
        await ctx.api.saveComposition(id, {
          composition, preset: query.preset || "",
        });
        draft.composition = {};
        // Re-render first: the flash node belongs to the page being replaced,
        // so flashing before the re-render writes into a node about to be
        // thrown away.
        await renderWorkbench(host, ctx, id, query);
        flash(host.querySelector("[data-flash]"),
          "Saved to this project's composition — applies to new sessions, and "
          + "to any running session you switch.");
      },
    });
  });

  host.querySelector("[data-raw]")?.addEventListener("click", () => {
    if (plugin) openPluginView(ctx.api, plugin);
  });

  // Duplicate-to-customise, from the page where you found out what this agent
  // does. This is the flagship: `explore` is locked, required and built in, and
  // pressing this yields a markdown file in which every one of those lines is
  // editable text — with `derived_from` as the only link back, because a live
  // one would recreate the coupling the locked tier exists to prevent.
  host.querySelector("[data-dup]")?.addEventListener("click", (e) => {
    duplicatePlugin(ctx, `agent.${id}`, e.target);
  });
}
