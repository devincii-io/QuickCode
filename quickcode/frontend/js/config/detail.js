// Level 1: one plugin, in full.
//
// The ladder is uniform — every configurable thing has a card (level 0), this
// page (level 1), a resolved view (level 2: the composed prompt range, the
// schema the model sees) and the raw definition (level 3, `openPluginView`
// unchanged). Locked plugins have all four rungs too; only the form in the
// middle is replaced by the Fixed-by-design block.
//
// The three-tier write protocol is untouched: free saves, confirm goes through
// the server's own 409 detail via `confirmRisk`, locked never reaches a PUT at
// all because it is not rendered as a control. That machinery lives in
// settings/fields.js and settings/ui.js and is reused verbatim.

import { esc } from "../util.js";
import { renderSettingsForm } from "../settings/fields.js";
import {
  chip, flash, highlightJson, openPluginView, splitError, tierBadge,
} from "../settings/ui.js";
import { explainHtml, fixedBlockHtml, recourseHtml } from "./explain.js";
import { bodyHtml, kindLabel, sigilHtml, signatureOf } from "./kinds.js";

function num(n) { return Number(n || 0).toLocaleString(); }

function headHtml(plugin, { crumb }) {
  return `<header class="cfg-head" data-kind="${esc(plugin.kind)}"
      data-tier="${esc(plugin.tier)}">
    <div class="cfg-crumbs">${crumb}</div>
    <div class="cfg-head-main">
      ${sigilHtml(plugin.kind, { big: true })}
      <h2>${esc(plugin.title || plugin.id)}</h2>
      <code class="cfg-id">${esc(plugin.id)}</code>
      <span class="cfg-head-badges">
        ${chip(kindLabel(plugin.kind), "k-" + plugin.kind)}
        ${chip(plugin.source, "src-" + plugin.source)}
        ${tierBadge(plugin.tier)}
      </span>
      <span class="cfg-head-actions">
        ${plugin.required
          ? `<span class="pc-required" title="This one holds the app together — it
              is always on. Everything about it is still readable.">permanent</span>`
          : `<label class="f-switch small" title="Enabled in this project">
               <input type="checkbox" data-enable${plugin.enabled ? " checked" : ""}>
               <span class="f-track"><span class="f-knob"></span></span></label>`}
        <button class="ghost-btn" data-raw title="Show the raw definition">Raw</button>
      </span>
    </div>
    <span class="set-flash" data-head-flash></span>
  </header>`;
}

/** Level 2 for the kinds that have one today: where a section lands in the
 *  composed prompt, and the exact schema a tool is declared with. */
function resolvedHtml(plugin, facts) {
  if (plugin.kind === "prompt_section") {
    const range = facts.ranges?.[plugin.id];
    return `<section class="cfg-sec">
      <h4>Where it lands</h4>
      ${range
        ? `<p class="cfg-note">Characters <b>${num(range.start)}–${num(range.end)}</b>
             of the composed prompt · ${num(range.end - range.start)} characters ·
             order #${esc(plugin.metadata?.order ?? "—")}.
             <a class="k-link" href="#/config/parts/prompt">Show it in the prompt →</a></p>`
        : `<p class="cfg-note">This section rendered empty for this session, so it
             is not in the composed prompt at all. It is still here to read —
             plan-mode and headless sections behave exactly this way.</p>`}
    </section>`;
  }
  if (plugin.kind === "tool") {
    return `<section class="cfg-sec" data-schema>
      <h4>The schema the model sees</h4>
      <div class="cfg-schema"><div class="set-loading">Reading the declaration…</div></div>
    </section>`;
  }
  return "";
}

export async function renderDetail(host, ctx, plugin, { crumb = "", lede = "" } = {}) {
  const free = (plugin.settings || []).filter((s) => s.tier !== "locked");
  const locked = (plugin.settings || []).filter((s) => s.tier === "locked");

  host.innerHTML = `<div class="cfg-detail">
    ${headHtml(plugin, { crumb })}
    ${lede ? `<div class="cfg-lede">${lede}</div>` : ""}
    <div class="cfg-summary">${bodyHtml(plugin, ctx.facts)}</div>
    ${explainHtml(plugin, { omitWhyFixed: locked.length > 0 })}
    ${free.length ? `<section class="cfg-sec"><h4>Settings</h4>
      <div class="cfg-form"></div></section>` : ""}
    ${locked.length
      ? fixedBlockHtml(plugin, locked)
      : plugin.tier === "locked" ? `<section class="k-fixed-block">
          <h4>Fixed by design</h4>
          <p class="k-fixed-why">${esc(plugin.locked_because
            || "Nothing here is a knob: this plugin is a contract the rest of the app is written against.")}</p>
          ${recourseHtml(plugin)}
        </section>` : ""}
    ${resolvedHtml(plugin, ctx.facts)}
  </div>`;

  const headFlash = host.querySelector("[data-head-flash]");

  if (free.length) {
    host.querySelector(".cfg-form").appendChild(
      renderSettingsForm({ ...plugin, settings: free }, {
        api: ctx.api,
        onUpdated: (fresh) => { Object.assign(plugin, fresh); ctx.touched?.(plugin); },
      }));
  }

  host.addEventListener("click", (e) => {
    if (e.target.closest("[data-raw]")) { openPluginView(ctx.api, plugin); return; }
    const rec = e.target.closest("[data-recourse]");
    if (rec) {
      const action = rec.dataset.recourse;
      if (action === "settings" && rec.dataset.target) ctx.go(`#/config/parts/${rec.dataset.target}`);
      else if (action === "duplicate" || action === "author") ctx.go("#/config/new/agent");
      else openPluginView(ctx.api, plugin);
    }
  });

  host.querySelector("[data-enable]")?.addEventListener("change", async (e) => {
    const box = e.target;
    try {
      const fresh = await ctx.api.updatePlugin(plugin.id, { enabled: box.checked });
      Object.assign(plugin, fresh);
      flash(headFlash, box.checked ? "Enabled." : "Disabled — takes effect in new sessions.");
      ctx.touched?.(plugin);
    } catch (err) {
      box.checked = plugin.enabled;
      flash(headFlash, splitError(err).detail, "err");
    }
  });

  // Level 2 for a tool: the declaration itself, from the same view payload the
  // raw inspector shows, so the signature on the card and the schema here can
  // never disagree.
  const schemaSlot = host.querySelector("[data-schema] .cfg-schema");
  if (schemaSlot) {
    try {
      const detail = await ctx.api.plugin(plugin.id);
      const content = detail.view?.content || "";
      ctx.facts.schemas[plugin.id] = signatureOf(content, plugin.title) || ctx.facts.schemas[plugin.id];
      schemaSlot.innerHTML = `<pre class="raw json">${highlightJson(content)}</pre>
        <div class="cfg-note">${num([...content].length)} characters of schema —
          this is what the model is told, verbatim.</div>`;
      const sig = ctx.facts.schemas[plugin.id];
      const sigSlot = host.querySelector(".cfg-summary .k-body");
      if (sig && sigSlot) sigSlot.textContent = sig;
    } catch (err) {
      schemaSlot.innerHTML = `<div class="set-error">Could not read the declaration:
        ${esc(err.message)}</div>`;
    }
  }
}
