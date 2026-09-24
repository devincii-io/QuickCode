// New composition: a named copy of one that exists, then the workbench.
//
// A composition is not a file under .quickcode/plugins/ — it is an entry under
// `presets` in the project's settings.json — so there is no template to write
// and no editor to open. What there is, is the same move as Duplicate
// everywhere else: pick the composition that is nearly right, name the copy,
// and land in the orchestrator's workbench to change what differs. The copy is
// written by `POST …/compositions/{id}/derive`, which refuses a name that is
// already taken rather than numbering it into one nobody typed.

import { esc } from "../../util.js";
import { flash, splitError } from "../../settings/ui.js";

/** `_composition_id` in server/agents_api.py, so the preview is the id that lands. */
export function compositionId(raw) {
  let text = String(raw || "").trim().toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").replace(/-{2,}/g, "-");
  if (text && !/^[a-z]/.test(text)) text = `c-${text}`;
  return text.slice(0, 48);
}

function optionHtml(p, chosen) {
  return `<label class="nw-radio"><input type="radio" name="nc-base" value="${esc(p.id)}"
      ${p.id === chosen ? "checked" : ""}>
    <span><b>${esc(p.title)}</b> <code>${esc(p.id)}</code>${
      p.builtin ? " · built in" : ""}<br>${esc(p.description || "")}</span></label>`;
}

export function renderNewComposition(host, ctx, query = {}) {
  const presets = ctx.presets?.presets || [];
  if (!presets.length) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
      read the compositions, so there is nothing to start from.</div></div>`;
    return;
  }
  const ids = new Set(presets.map((p) => p.id));
  const chosen = ids.has(query.from) ? query.from
    : ids.has(ctx.presets.active) ? ctx.presets.active : presets[0].id;

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head" data-kind="agent">
      <div class="cfg-crumbs"><a href="#/config/compositions">Compositions</a> ▸
        New composition</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="agent">@</span>
        <h2>New composition</h2>
      </div>
    </header>
    <div class="cfg-lede">A composition is the orchestrator's configuration under
      a name you can switch to: its tools, the agents it may spawn, the prompt
      sections it rewrites and the mode it starts in. It is written into this
      project's <code>.quickcode/settings.json</code> under <code>presets</code>,
      starting as an exact copy of the one you pick; you then change what differs
      in the orchestrator's workbench.</div>

    <section class="cfg-sec">
      <h4>Start from</h4>
      <div class="nw-form">
        <fieldset class="nw-field">
          ${presets.map((p) => optionHtml(p, chosen)).join("")}
        </fieldset>
        <label class="nw-field">
          <span>Name</span>
          <input class="tp-input" data-name spellcheck="false" autocomplete="off"
                 placeholder="review-only">
          <span class="nw-help">Shown as typed; stored under the id below. A name
            that is already taken is refused, not renumbered.</span>
        </label>
        <div class="nw-target">Writes <code data-target>—</code></div>
        <div class="nw-actions">
          <button class="btn primary" data-create disabled>Create and open</button>
          <span class="set-flash" data-flash></span>
        </div>
      </div>
      <p class="cfg-note">Nothing running changes: a session keeps the composition
        it opened with until you switch it. Making this one the default for new
        sessions is a separate choice, on the Compositions page.</p>
    </section>
  </div>`;

  const nameEl = host.querySelector("[data-name]");
  const targetEl = host.querySelector("[data-target]");
  const createEl = host.querySelector("[data-create]");
  const flashEl = host.querySelector("[data-flash]");
  const baseOf = () => host.querySelector("input[name=nc-base]:checked")?.value || chosen;

  const paint = () => {
    const id = compositionId(nameEl.value);
    targetEl.textContent = id ? `presets.${id}` : "—";
    createEl.disabled = !id;
  };
  paint();
  nameEl.addEventListener("input", paint);
  nameEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !createEl.disabled) createEl.click();
  });
  nameEl.focus();

  createEl.addEventListener("click", async () => {
    createEl.disabled = true;
    createEl.textContent = "Writing…";
    try {
      const made = await ctx.api.deriveComposition(baseOf(), nameEl.value.trim());
      ctx.invalidate?.();
      ctx.go(`#/config/agents/%40orchestrator?preset=${encodeURIComponent(made.id)}`);
    } catch (err) {
      createEl.disabled = false;
      createEl.textContent = "Create and open";
      flash(flashEl, splitError(err).detail, "err");
    }
  });
}
