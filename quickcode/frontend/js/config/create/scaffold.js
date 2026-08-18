// Creation and editing: New…, the raw source editor, and Duplicate.
//
// **The raw editor is the primary surface, not the escape hatch.** For every
// authorable kind the interesting part is the body — an agent's instructions, a
// prompt section's text, a tool's prose description and its argv template — and
// a body is a text file. A form that owns the file and offers "advanced: edit
// as text" gets this backwards: it makes the 80% case (typing prose) the
// awkward one, and it means the form has to be able to represent every file it
// might be handed, which it cannot.
//
// So: the file is the document, the textarea is the editor, and the panel
// beside it is a *reading* of what is currently typed — plus, for the two or
// three keys where a control genuinely beats typing, a small field that
// rewrites exactly one frontmatter line and leaves the rest of the bytes alone.
// Switching between them is free in both directions for as long as the content
// parses, which is the property the plan asks for.
//
// Saving never refuses. `PUT .../source` writes first and validates second, so
// a half-finished file is allowed to exist on disk; what you get back is the
// list of problems, shown inline immediately. The alternative — an editor that
// will not let you stop typing halfway — is worse.
//
// Everything here writes a file. Nothing here changes a running session: a
// session's composition is frozen when it opens, so every success message says
// "takes effect in new sessions" and means it.

import { esc } from "../../util.js";
import { chip, flash, splitError } from "../../settings/ui.js";
import { problemsCardHtml } from "../problems.js";
import { agentPanel } from "./agent.js";
import { toolPanel } from "./tool.js";
import { promptPanel } from "./prompt.js";

export const APPLIES = "Takes effect in new sessions — a running session's "
  + "composition is frozen when it opens, and nothing hot-swaps into it.";

export const AUTHORABLE = [
  ["agent", "New agent", "@",
   `A subagent: what it may call, which models it may run on, how far it may go,
    and its instructions as the body. The instructions are the point; everything
    above them is four lines of frontmatter.`],
  ["tool", "New command tool", "fn",
   `A tool the model can call that runs one command you pin down —
    <code>uv run pytest -q {path}</code> rather than an open shell. The command
    is an argv array executed directly, so a value containing
    <code>; rm -rf /</code> is one inert argument: nothing ever parses it.`],
  ["prompt", "New prompt section", "¶",
   `A block of the system prompt of your own, with an <code>after:</code> naming
    the section it follows and an <code>applies_to:</code> deciding whether it
    reaches subagents too.`],
];

const PANELS = { agent: agentPanel, tool: toolPanel, prompt: promptPanel };

// ---- text utilities -------------------------------------------------------
//
// These mirror `kernel/authoring/store.py` and `format.py`. They are advisory
// only: every one of them is used to *describe* what is typed, never to decide
// whether it is valid. The server is the validator, always, and its answer
// arrives with the save.

/** `_slugify` from store.py, so the path preview matches the file that lands. */
export function slugify(raw) {
  let text = String(raw || "").trim().toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").replace(/-{2,}/g, "-");
  if (text && !/^[a-z]/.test(text)) text = `p-${text}`;
  return text.slice(0, 32);
}

/** The frontmatter block as {key: value}, plus where it ends. */
export function parseFrontmatter(text) {
  const lines = String(text || "").split("\n");
  if (lines[0]?.trim() !== "---") return { meta: {}, close: -1, lines };
  const close = lines.findIndex((l, i) => i > 0 && l.trim() === "---");
  if (close < 0) return { meta: {}, close: -1, lines };
  const meta = {};
  let key = "";
  for (let i = 1; i < close; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const m = /^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$/.exec(line);
    if (m) { key = m[1]; meta[key] = m[2].trim(); }
    else if (key && /^\s+/.test(line)) meta[key] = `${meta[key]} ${line.trim()}`.trim();
  }
  return { meta, close, lines };
}

/** `[a, b]` → ["a","b"]; a bare word → ["a"]. */
export function parseList(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return [];
  const inner = raw.startsWith("[") && raw.endsWith("]") ? raw.slice(1, -1) : raw;
  return inner.split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean);
}

/** The first ```json <tag>``` fence, parsed. `null` when absent or broken —
 *  the panel says which of the two it is rather than guessing. */
export function fencedJson(text, tag) {
  const re = new RegExp("```json\\s+" + tag + "\\s*\\n([\\s\\S]*?)```", "m");
  const m = re.exec(String(text || ""));
  if (!m) return { found: false, value: null, error: "" };
  try {
    return { found: true, value: JSON.parse(m[1]), error: "" };
  } catch (err) {
    return { found: true, value: null, error: err.message };
  }
}

/** Rewrite one frontmatter line in place, adding it if it is absent. The rest
 *  of the file is untouched byte for byte — this is what makes the small
 *  fields beside the editor safe to use on a file somebody hand-wrote. */
export function patchFrontmatter(text, key, value) {
  const { close, lines } = parseFrontmatter(text);
  if (close < 0) return text;
  const line = `${key}: ${value}`;
  for (let i = 1; i < close; i += 1) {
    if (new RegExp(`^${key}\\s*:`).test(lines[i])) {
      lines[i] = line;
      return lines.join("\n");
    }
  }
  lines.splice(close, 0, line);
  return lines.join("\n");
}

// ---- the shared write actions --------------------------------------------

/** Duplicate-to-customise. One press, one file, lands you in the editor.
 *
 *  Refusals are the interesting path: `POST .../duplicate` answers 400 with
 *  the reason *and* the recourse in its detail, and both are rendered rather
 *  than collapsed into "could not duplicate". */
export async function duplicatePlugin(ctx, id, btn = null, { scope = "project" } = {}) {
  const label = btn?.textContent;
  if (btn) { btn.disabled = true; btn.textContent = "Duplicating…"; }
  try {
    const made = await ctx.api.duplicatePlugin(id, { scope });
    const newId = made.plugin?.id;
    ctx.invalidate?.();
    if (newId) {
      ctx.go(`#/config/edit/${encodeURIComponent(newId)}?from=${encodeURIComponent(id)}`);
    } else {
      ctx.go("#/config/problems");
    }
    return made;
  } catch (err) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
      showRefusal(btn, splitError(err).detail);
    }
    return null;
  }
}

/** The refusal, in place of the button, with its recourse as a real button.
 *  A 400 here is not an error condition — it is the app explaining a design
 *  decision, and it deserves the space. */
function showRefusal(btn, detail) {
  const holder = btn.closest(".k-card-side, .cfg-head-actions, .empty-actions, .k-recourse")
    || btn.parentElement;
  if (!holder) return;
  const existing = holder.parentElement?.querySelector(".dup-refusal");
  existing?.remove();
  const node = document.createElement("div");
  node.className = "dup-refusal";
  // The recourse is already inside the detail — the server sends the reason
  // and the fix as one sentence pair. The button is only added when the fix
  // names something this view can actually open; inventing one for a kind the
  // server said has no recourse would put a dead end behind a live-looking
  // button.
  node.innerHTML = `<p class="dup-why">${esc(detail)}</p>
    ${detail.includes("New command tool")
      ? `<a class="btn" href="#/config/new/tool">+ New command tool</a>` : ""}`;
  (holder.parentElement || holder).appendChild(node);
  btn.remove();
}

// ---- New… -----------------------------------------------------------------

export function renderNew(host, ctx, kind) {
  if (kind === "composition") { renderNewComposition(host); return; }
  const entry = AUTHORABLE.find(([k]) => k === kind) || AUTHORABLE[0];
  const [k, title, sigil, what] = entry;
  const dirs = ctx.dirs || {};

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head" data-kind="${k === "prompt" ? "prompt_section" : esc(k)}">
      <div class="cfg-crumbs"><a href="#/config/parts/tools">New</a> ▸ ${esc(title)}</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="${k === "prompt" ? "prompt_section" : esc(k)}"
          >${esc(sigil)}</span>
        <h2>${esc(title)}</h2>
      </div>
    </header>
    <div class="cfg-lede">${what}</div>

    <section class="cfg-sec">
      <h4>Name it</h4>
      <div class="nw-form">
        <label class="nw-field">
          <span>Name</span>
          <input class="tp-input" data-name spellcheck="false" autocomplete="off"
                 placeholder="${k === "tool" ? "pytest-failed"
                   : k === "agent" ? "reviewer" : "house-style"}">
          <span class="nw-help">Lower case, digits, <code>-</code> and
            <code>_</code>. This becomes the id and the file name, and for a
            tool it is the name the model calls.</span>
        </label>
        <label class="nw-field">
          <span>Title</span>
          <input class="tp-input" data-title spellcheck="false" autocomplete="off"
                 placeholder="optional — shown on the card">
        </label>
        <fieldset class="nw-field">
          <span>Scope</span>
          <label class="nw-radio"><input type="radio" name="nw-scope" value="project" checked>
            <span><b>This project</b> — <code>${esc(dirs.project || ".quickcode/plugins/")}</code><br>
              committed, so it travels with the repository and applies to
              everyone who clones it.</span></label>
          <label class="nw-radio"><input type="radio" name="nw-scope" value="user">
            <span><b>Every project</b> — <code>${esc(dirs.user || "~/.quickcode/plugins/")}</code><br>
              yours alone, and it follows you into every project you open.</span></label>
        </fieldset>
        <div class="nw-target">Writes <code data-target>—</code></div>
        <div class="nw-actions">
          <button class="btn primary" data-create disabled>Create and open</button>
          <span class="set-flash" data-flash></span>
        </div>
      </div>
      <p class="cfg-note">It starts as a commented example that already loads —
        a real file, not a stub — and opens in the editor. ${esc(APPLIES)}</p>
    </section>
  </div>`;

  const nameEl = host.querySelector("[data-name]");
  const titleEl = host.querySelector("[data-title]");
  const targetEl = host.querySelector("[data-target]");
  const createEl = host.querySelector("[data-create]");
  const flashEl = host.querySelector("[data-flash]");
  const scopeOf = () => host.querySelector("input[name=nw-scope]:checked").value;

  const paint = () => {
    const slug = slugify(nameEl.value);
    const dir = scopeOf() === "user"
      ? (dirs.user || "~/.quickcode/plugins") : (dirs.project || ".quickcode/plugins");
    // The separator comes from the path the server sent, not from a guess:
    // a Windows directory with a forward slash appended reads as a typo and
    // makes people doubt the whole preview.
    const sep = dir.includes("\\") ? "\\" : "/";
    targetEl.textContent = slug
      ? `${dir}${dir.endsWith(sep) ? "" : sep}${slug}.md` : "—";
    createEl.disabled = !slug;
  };
  paint();
  nameEl.addEventListener("input", paint);
  for (const r of host.querySelectorAll("input[name=nw-scope]")) {
    r.addEventListener("change", paint);
  }
  nameEl.focus();

  createEl.addEventListener("click", async () => {
    createEl.disabled = true;
    createEl.textContent = "Writing…";
    try {
      const made = await ctx.api.createAuthored({
        kind: k, name: nameEl.value, title: titleEl.value, scope: scopeOf(),
      });
      ctx.invalidate?.();
      ctx.go(`#/config/edit/${encodeURIComponent(made.plugin?.id || `${k}.${slugify(nameEl.value)}`)}`);
    } catch (err) {
      createEl.disabled = false;
      createEl.textContent = "Create and open";
      flash(flashEl, splitError(err).detail, "err");
    }
  });
  nameEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !createEl.disabled) createEl.click();
  });
}

function renderNewComposition(host) {
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">New composition</div>
      <div class="cfg-head-main"><h2>New composition</h2></div>
    </header>
    <section class="cfg-soon">
      <h4>Start from one that exists</h4>
      <p>A composition is a named set: the orchestrator's tools and prompt, the
        agents it may spawn, and the bindings that attach parts to them. It lives
        in <code>.quickcode/settings.json</code> under <code>presets</code>, not
        in <code>.quickcode/plugins/</code>, so it is not one of the file kinds
        this page writes.</p>
      <p class="cfg-note">Open any composition and press <b>Customise this…</b>:
        it derives a project-scoped copy you can edit, which is the same
        duplicate-to-customise move as everywhere else.
        <a class="k-link" href="#/config/compositions">Compositions →</a></p>
    </section>
  </div>`;
}

// ---- the editor -----------------------------------------------------------

export async function renderEditor(host, ctx, id, query = {}) {
  host.innerHTML = `<div class="cfg-page-inner"><div class="set-loading">Reading
    ${esc(id)}…</div></div>`;

  let source;
  try {
    source = await ctx.api.authoredSource(id);
  } catch (err) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
      open <code>${esc(id)}</code>: ${esc(splitError(err).detail)}<br>
      Only authored plugins have a file. Everything built into QuickCode is
      Python, and the way to get an editable version of one is
      <b>Duplicate</b>.</div></div>`;
    return;
  }

  // The authored list is the registry's view, and a plugin the registry
  // skipped — a project's command tools before the project is trusted, a file
  // with an error in it — is legitimately not in it. The file itself is always
  // readable, so the header falls back to what the file says about itself
  // rather than to the bare id.
  const listed = (ctx.authored || []).find((p) => p.id === id) || {};
  const written = parseFrontmatter(source.text).meta;
  const heading = listed.title || written.title || written.name || id;
  const kind = id.split(".")[0];
  const state = { text: source.text, saved: source.text, problems: source.problems || [] };

  host.innerHTML = `<div class="ed">
    <div class="ed-main">
      <header class="cfg-head" data-kind="${kind === "prompt" ? "prompt_section" : esc(kind)}">
        <div class="cfg-crumbs"><a href="#/config/parts/tools">Yours</a> ▸
          ${esc(heading)}</div>
        <div class="cfg-head-main">
          <h2>${esc(heading)}</h2>
          <code class="cfg-id">${esc(id)}</code>
          <span class="cfg-head-badges">
            ${chip("yours", "src-config")}
            ${chip(listed.scope === "user" ? "every project" : "this project")}
            ${listed.id ? "" : chip("not loaded", "src-config")}
            ${listed.derived_from || query.from
              ? chip(`from ${listed.derived_from || query.from}`) : ""}
          </span>
          <span class="cfg-head-actions">
            <button class="ghost-btn" data-delete title="Moves the file to
              .quickcode/plugins/.trash/ — nothing is destroyed">Delete</button>
          </span>
        </div>
        <div class="ed-path"><code>${esc(source.path)}</code></div>
      </header>

      <textarea class="ed-text" spellcheck="false" wrap="off"></textarea>

      <div class="ed-bar">
        <button class="btn primary" data-save disabled>Save</button>
        <button class="btn" data-revert disabled>Revert</button>
        <span class="ed-state" data-state></span>
        <span class="set-flash" data-flash></span>
        <span class="ed-applies">${esc(APPLIES)}</span>
      </div>

      <div class="ed-problems"></div>
    </div>
    <div class="ed-side"></div>
  </div>`;

  const textEl = host.querySelector(".ed-text");
  const saveEl = host.querySelector("[data-save]");
  const revertEl = host.querySelector("[data-revert]");
  const stateEl = host.querySelector("[data-state]");
  const flashEl = host.querySelector("[data-flash]");
  const problemsEl = host.querySelector(".ed-problems");
  const sideEl = host.querySelector(".ed-side");
  textEl.value = state.text;

  const panel = (PANELS[kind] || promptPanel)({
    ctx, id, listed,
    read: () => textEl.value,
    // A field beside the editor writes back into the same bytes and nothing
    // else, so "edit as file" stays reversible in both directions.
    write: (next) => { textEl.value = next; onInput(); },
  });
  sideEl.innerHTML = panel.html();
  panel.mount?.(sideEl);

  const paintProblems = () => {
    problemsEl.innerHTML = state.problems.length
      ? problemsCardHtml(state.problems, {
          title: "This file",
          note: `Reported by the validator that the runtime itself uses. An
            <b>error</b> means this file contributes nothing until it is fixed —
            it is not loaded, and it is not in the plugin list.`,
        })
      : `<div class="ed-clean">No problems. This file loads.</div>`;
  };

  const paintState = () => {
    const dirty = textEl.value !== state.saved;
    saveEl.disabled = !dirty;
    revertEl.disabled = !dirty;
    stateEl.textContent = dirty ? "unsaved changes" : "saved";
    stateEl.dataset.dirty = dirty ? "1" : "";
  };

  function onInput() {
    paintState();
    sideEl.innerHTML = panel.html();
    panel.mount?.(sideEl);
  }

  paintProblems();
  paintState();
  textEl.addEventListener("input", onInput);
  textEl.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(); }
  });
  revertEl.addEventListener("click", () => { textEl.value = state.saved; onInput(); });
  saveEl.addEventListener("click", save);

  async function save() {
    saveEl.disabled = true;
    const body = textEl.value;
    try {
      const res = await ctx.api.saveAuthoredSource(id, body);
      state.saved = body;
      state.problems = res.problems || [];
      paintProblems();
      paintState();
      ctx.invalidate?.();
      const errors = state.problems.filter((p) => p.severity === "error").length;
      flash(flashEl, errors
        ? `Written — but it does not load yet. ${errors} error${errors === 1 ? "" : "s"} below.`
        : `Saved. ${res.applies_to === "new sessions" ? APPLIES : ""}`,
        errors ? "err" : "ok");
    } catch (err) {
      paintState();
      flash(flashEl, splitError(err).detail, "err");
    }
  }

  host.querySelector("[data-delete]")?.addEventListener("click", async (e) => {
    const btn = e.target;
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "Move to .trash/ — press again";
      setTimeout(() => {
        if (!btn.isConnected) return;
        btn.dataset.armed = ""; btn.textContent = "Delete";
      }, 4000);
      return;
    }
    try {
      await ctx.api.deleteAuthored(id);
      ctx.invalidate?.();
      // Back to the page this plugin was listed on, so the deletion is visible
      // as an absence from the list rather than as a message about one.
      ctx.go(kind === "agent" ? "#/config/agents"
        : kind === "prompt" ? "#/config/parts/prompt" : "#/config/parts/tools");
    } catch (err) {
      flash(flashEl, splitError(err).detail, "err");
    }
  });
}
