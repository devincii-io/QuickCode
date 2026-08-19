// Permission profiles — a named posture: how much may this session do on its
// own, right now.
//
// Its own page rather than a section of Compositions, because the two answer
// different questions about the same session. A composition says *which agent
// you are talking to* — its tools, its prompt, the subagents it may spawn. A
// profile says *how much it may do without asking*. The same composition is
// worth running under "Read only" while you read a pull request and under
// "Build and test" ten minutes later, and nothing about the agent changes in
// between. (`quickcode/core/profiles.py` opens with the same distinction.)
//
// Two things the module decided that this page has to keep visible, because a
// UI that hid either would be lying about what the picker does:
//
//   * A profile's rules **merge** with the project's own — they never replace
//     them. So a profile narrows by saying `deny`, never by leaving something
//     out, and the editor says so beside the deny box rather than in a doc.
//   * `mode` is where a session **starts**, not a ceiling. Shift+Tab still
//     works afterwards, which is why "Read only" denies `write` outright
//     instead of trusting plan mode to still be the mode in ten minutes.
//
// The refusal rendering is the other half of the point. A profile a project
// authored and the trust gate reduced must *look* reduced in the list — the
// user picked it by name and expects the name to hold, so a warning in a log
// line somewhere else is not good enough.

import { esc } from "../util.js";
import { flash } from "../settings/ui.js";
import { problemsCardHtml, wireProblems } from "./problems.js";
import { characterToSpec, evaluate } from "../help/engine.js";

const LISTS = [
  ["allow", "Allow", "Runs without a prompt. This is the widening half — a "
    + "project nobody has trusted may not state one at all."],
  ["ask", "Ask", "Always prompts, even when an allow rule would have matched: "
    + "ask is checked first, so it carves exceptions out of a blanket grant."],
  ["deny", "Deny", "Refused outright, before everything else. Because a "
    + "profile's rules merge with the project's own rather than replacing "
    + "them, this is the only way a profile narrows anything."],
];

const MODES = [
  ["plan", "Plan — read-only exploration; the agent submits a plan first"],
  ["ask", "Ask — every mutating action asks for permission"],
  ["auto-edit", "Auto-edit — edits inside the project run; the shell still asks"],
  ["dontask", "Don't ask — never prompts; anything outside the rules is denied"],
  ["yolo", "Yolo — no prompts at all (and only if the app was launched with --yolo)"],
];

const LAYER_NOTE = {
  default: "shipped with QuickCode",
  user: "yours, in ~/.quickcode/settings.json",
  project: "this project's, in .quickcode/settings.json",
};

// The same grammar `core/profiles._RULE_SHAPE` rejects on: a bare tool name, or
// a tool name with a pattern in brackets. Anything else the engine silently
// reads as a tool nothing is called, so it matches nothing, forever, quietly.
const RULE_SHAPE = /^\w+(\([\s\S]*\))?$/;

const lines = (text) => String(text || "").split("\n")
  .map((s) => s.trim()).filter(Boolean);

/** Live tools with their declared shape, exactly as the Help sandbox reads
 *  them: `metadata.character` is derived by the kernel from the tool's real
 *  PermissionSpec, so this is the tool's own declaration rather than a table
 *  maintained here. */
function toolChoices(kernel) {
  return (kernel?.plugins || [])
    .filter((p) => p.kind === "tool")
    .map((p) => ({
      name: p.metadata?.tool_name || p.title || p.id.replace(/^tool\./, ""),
      character: p.metadata?.character || "",
    }))
    .filter((t) => t.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

// ---- the list -------------------------------------------------------------

function badge(profile, active) {
  return `<span class="k-badges">
    <span class="pf-layer" data-layer="${esc(profile.layer)}"
      title="${esc(LAYER_NOTE[profile.layer] || "")}">${esc(profile.layer)}</span>
    ${profile.id === active ? `<span class="pv-active">✓ active</span>` : ""}
  </span>`;
}

/** What the trust gate and the validator did to this profile, on the profile.
 *  The wording is the module's own — `problems()` says the same thing to the
 *  Problems list, and two accounts of one refusal that differ by a word read as
 *  two different refusals. */
function reducedHtml(p) {
  const out = [];
  if ((p.refused || []).length) {
    out.push(`<div class="pf-reduced" data-kind="refused">
      <b>Reduced.</b> This project is not trusted, so
      ${esc(p.refused.join(" and "))} ${p.refused.length === 1 ? "was" : "were"}
      ignored. It can narrow what the agent may do here, not widen it — trust
      the project to let the rest of it apply.</div>`);
  }
  if ((p.invalid || []).length) {
    out.push(`<div class="pf-reduced" data-kind="invalid">
      <b>Partly dropped.</b> ${p.invalid.length}
      ${p.invalid.length === 1 ? "entry" : "entries"} the permission engine can
      never match (${esc(p.invalid.join(", "))}). The rest of the profile
      applies.</div>`);
  }
  return out.join("");
}

/** Why "Use this" is greyed. Selecting a profile that lets the agent act
 *  without asking is gated on project trust — the selection lives in the
 *  project's settings file, where nothing can tell it from the same line
 *  committed by the repository. Said on the card rather than discovered on the
 *  click, and with the decision itself one button away. */
function blockedHtml() {
  return `<div class="pf-reduced" data-kind="untrusted">
    <b>Needs trust.</b> This profile lets the agent act without asking, and this
    project has not been trusted, so selecting it would have no effect. Profiles
    that only narrow — Read only, Survey — work in any project.
    <button class="ghost-btn" data-trust>Trust this project</button></div>`;
}

function ruleCount(p) {
  const bits = LISTS
    .map(([key, label]) => [(p[key] || []).length, label])
    .filter(([n]) => n)
    .map(([n, label]) => `${n} ${label.toLowerCase()}`);
  return bits.join(" · ") || "no rules of its own";
}

function card(p, active, trusted) {
  const builtin = p.layer === "default";
  const blocked = !trusted && p.widens && p.id !== active;
  return `<article class="k-card pf-card${p.id === active ? " is-active" : ""}"
      data-id="${esc(p.id)}" data-layer="${esc(p.layer)}">
    <div class="k-card-main">
      <div class="k-card-head">
        <span class="k-sigil" data-kind="policy">§</span>
        <span class="k-title">${esc(p.title)}</span>
        <code class="k-id">${esc(p.id)}</code>
        ${badge(p, active)}
      </div>
      <div class="k-summary">${esc(p.description || "")}</div>
      ${reducedHtml(p)}
      ${blocked ? blockedHtml() : ""}
      <div class="k-facts">
        <span class="k-fact">starts in <code>${esc(p.mode)}</code></span>
        <span class="k-fact">${esc(ruleCount(p))}</span>
      </div>
    </div>
    <div class="k-card-side">
      ${p.id === active
        ? `<button class="btn" data-clear>Stop using it</button>`
        : blocked
          ? `<button class="btn" disabled>Use this</button>`
          : `<button class="btn" data-use="${esc(p.id)}">Use this</button>`}
      <button class="ghost-btn" data-duplicate="${esc(p.id)}"
        title="Copy it into one of your own, under a new id">Duplicate</button>
      ${builtin ? "" : `<a class="ghost-btn"
        href="#/config/profiles/${encodeURIComponent(p.id)}?scope=${esc(p.layer)}"
        >Edit</a>
      <button class="ghost-btn danger" data-delete="${esc(p.id)}"
        data-scope="${esc(p.layer)}">Delete</button>`}
    </div>
  </article>`;
}

// ---- the editor -----------------------------------------------------------

function listBox(key, label, note, value) {
  return `<div class="pf-list">
    <label for="pf-${key}">${esc(label)}</label>
    <textarea id="pf-${key}" data-list="${key}" rows="5" spellcheck="false"
      placeholder="one rule per line — bash(git **), read(src/**), write"
      >${esc((value || []).join("\n"))}</textarea>
    <div class="pf-note">${esc(note)}</div>
    <div class="pf-bad" data-bad="${key}"></div>
  </div>`;
}

function editorHtml(draft, { tools, scope, isNew, builtinIds }) {
  const shadowing = builtinIds.has(draft.id);
  return `<section class="cfg-sec pf-editor">
    <h3>${isNew ? "New profile" : `Editing ${esc(draft.title)}`}</h3>

    <div class="set-field">
      <label for="pf-id">Id</label>
      <input id="pf-id" spellcheck="false" autocomplete="off"
        value="${esc(draft.id)}"${isNew ? "" : " readonly"}
        placeholder="git-and-tests">
      <div class="pf-note">The key it is stored under, and what
        <code>active_profile</code> names. ${isNew
          ? "Letters, digits, dots, dashes and underscores."
          : "Changing it would write a second profile rather than rename this one, so it is fixed here — duplicate instead."}</div>
      ${shadowing ? `<div class="pf-warn">There is a built-in called
        <code>${esc(draft.id)}</code>. Saving under this id writes a copy that
        hides it everywhere, under the same name; the server asks before it does
        that. Deleting the copy brings the built-in back.</div>` : ""}
    </div>

    <div class="set-field">
      <label for="pf-title">Title</label>
      <input id="pf-title" spellcheck="false" value="${esc(draft.title)}"
        placeholder="Git and tests">
    </div>

    <div class="set-field">
      <label for="pf-description">Description</label>
      <textarea id="pf-description" rows="3" spellcheck="false"
        placeholder="What it is for, and when you would pick it."
        >${esc(draft.description)}</textarea>
    </div>

    <div class="set-field">
      <label for="pf-mode">Starting mode</label>
      <select id="pf-mode">${MODES.map(([id, text]) =>
        `<option value="${id}"${id === draft.mode ? " selected" : ""}>${esc(text)}</option>`
      ).join("")}</select>
      <div class="pf-note">Where a session <em>starts</em>, not a ceiling —
        Shift+Tab still works afterwards. A profile that means to hold has to
        say so in its deny list.</div>
    </div>

    <div class="set-field">
      <label for="pf-scope">Saved in</label>
      <select id="pf-scope">
        <option value="user"${scope === "user" ? " selected" : ""}
          >Your settings — every project on this machine</option>
        <option value="project"${scope === "project" ? " selected" : ""}
          >This project — .quickcode/settings.json, shared with whoever clones it</option>
      </select>
      <div class="pf-note">A project's allow rules and permissive modes are
        inert until you trust the project once; its deny and ask rules always
        apply, because narrowing needs nobody's consent.</div>
    </div>

    <div class="pf-lists">${LISTS.map(([key, label, note]) =>
      listBox(key, label, note, draft[key])).join("")}</div>

    <details class="pf-tools">
      <summary>Rule syntax and the tool names in this install (${tools.length})</summary>
      <p class="pf-note">A rule is a tool name — <code>write</code> — or a tool
        name with a pattern in brackets — <code>bash(git **)</code>,
        <code>read(src/**)</code>. In a pattern <code>**</code> crosses
        directories and <code>*</code> does not, so <code>read(src/*)</code>
        matches <code>src/a.py</code> and not <code>src/lib/a.py</code>. The
        pattern is matched whole, against the path for a file tool and against
        the command line for the shell.</p>
      <div class="pf-toolchips">${tools.map((t) =>
        `<code class="pf-chip" title="${esc(t.character || "shape not declared")}"
          >${esc(t.name)}</code>`).join("")}</div>
    </details>

    <div class="f-actions">
      <button class="btn primary" data-save>Save profile</button>
      <a class="ghost-btn" href="#/config/profiles">Cancel</a>
      <span class="set-flash" data-flash></span>
    </div>
  </section>`;
}

// ---- the live preview -----------------------------------------------------
//
// The one computed answer on this page. `help/engine.js` is a line-for-line
// port of `core/permissions.py` and already backs the Help sandbox; reusing it
// means a rule can be checked while it is being typed rather than by saving,
// switching to it and running something. It is *modelled*, and says so in the
// sandbox's own words — the honest caveat is the same one, so the two pages do
// not appear to make different promises about the same code.

function previewHtml(tools, mode) {
  const opts = tools.map((t) =>
    `<option value="${esc(t.name)}" data-character="${esc(t.character)}"
      >${esc(t.name)}</option>`).join("");
  return `<section class="cfg-sec pf-preview">
    <h3>What would this decide?</h3>
    <div class="pf-try">
      <div class="set-field">
        <label for="pf-try-tool">Tool</label>
        <select id="pf-try-tool">${opts}</select>
      </div>
      <div class="set-field pf-try-target">
        <label for="pf-try-target">Acting on</label>
        <input id="pf-try-target" spellcheck="false" autocomplete="off"
          value="git status" placeholder="a path, or a command line">
      </div>
      <div class="set-field">
        <label for="pf-try-mode">In mode</label>
        <select id="pf-try-mode">${MODES.map(([id]) =>
          `<option value="${id}"${id === mode ? " selected" : ""}>${id}</option>`
        ).join("")}</select>
      </div>
    </div>
    <div class="pf-verdict" data-verdict aria-live="polite"></div>
    <p class="hp-honesty">Modelled in the browser: the rule syntax, the glob
      matching, the ordering and the bash decomposition are ported from
      quickcode/core/permissions.py, and the tool list and each tool's declared
      shape are read live from this install. The one thing the browser cannot
      reproduce is real path resolution — the running engine resolves the target
      against the project on disk, so it also catches a symlink pointing outside
      it, which this cannot. It also sees only this profile's rules, not the
      project's own that they merge with.</p>
  </section>`;
}

const OUTCOME = { allow: "runs without asking", ask: "prompts you", deny: "refused" };

function renderVerdict(node, { tools, rules, tool, target, mode }) {
  const character = tools.find((t) => t.name === tool)?.character || "";
  const result = evaluate({
    mode, tool, spec: characterToSpec(character), target, rules,
  });
  node.innerHTML = `<div class="pf-outcome" data-outcome="${esc(result.decision)}">
      <span class="pf-outcome-word">${esc(result.decision)}</span>
      <span class="pf-outcome-say">${esc(tool)} on
        <code>${esc(target || "(nothing)")}</code> ${esc(OUTCOME[result.decision])}</span>
    </div>
    <ol class="pf-trace">${result.trace.map((s) =>
      `<li data-hit="${esc(String(s.hit))}"><b>${esc(s.name)}</b> ${esc(s.why)}</li>`
    ).join("")}</ol>`;
}

// ---- page -----------------------------------------------------------------

function emptyDraft(id = "") {
  return { id, title: "", description: "", mode: "ask", allow: [], ask: [], deny: [] };
}

/** A copy of `p` under a free id, which is what makes customising a built-in a
 *  two-click path instead of retyping thirteen deny rules. */
function duplicateOf(p, taken) {
  let id = `${p.id}-copy`;
  let n = 2;
  while (taken.has(id)) id = `${p.id}-copy-${n++}`;
  return {
    id,
    title: `${p.title} (copy)`,
    description: p.description || "",
    mode: p.mode,
    allow: [...(p.allow || [])],
    ask: [...(p.ask || [])],
    deny: [...(p.deny || [])],
  };
}

export async function renderProfiles(host, ctx, selected = "", query = {}) {
  host.innerHTML = `<div class="cfg-page-inner"><div class="set-loading">Reading
    the permission profiles…</div></div>`;

  let data;
  try {
    data = await ctx.api.profiles();
  } catch (err) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">Could not
      read the permission profiles: ${esc(err.message)}</div></div>`;
    return;
  }

  const profiles = data.profiles || [];
  const byId = new Map(profiles.map((p) => [p.id, p]));
  const builtinIds = new Set(profiles.filter((p) => p.layer === "default").map((p) => p.id));
  const tools = toolChoices(ctx.kernel);

  // Three shapes behind one route: the list, a new profile, and an existing one
  // opened for editing. `?from=` is Duplicate, which is "new, prefilled".
  const chosen = selected && selected !== "new" ? byId.get(selected) : null;
  if (selected && selected !== "new" && !chosen) {
    host.innerHTML = `<div class="cfg-page-inner"><div class="set-error">There is
      no permission profile <code>${esc(selected)}</code>.</div></div>`;
    return;
  }
  // A built-in has no file to edit. Saving over its id writes a *shadow* that
  // hides it under the same title, which is not what clicking its name means —
  // so opening one opens a copy of it instead. That is also the flow this page
  // exists to make short: customising a built-in should be two clicks, not
  // thirteen retyped deny rules.
  const duplicating = chosen?.layer === "default";
  const isNew = selected === "new" || duplicating;
  const source = duplicating ? chosen : (query.from ? byId.get(query.from) : null);
  let draft = isNew
    ? (source ? duplicateOf(source, new Set(byId.keys())) : emptyDraft())
    : chosen;
  if (draft && !isNew) draft = { ...draft };
  const editing = !!draft;
  const scope = query.scope === "project" ? "project" : (draft?.layer || "user");

  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">${editing
        ? `<a href="#/config/profiles">Permission profiles</a> ▸ ${esc(
            source ? `copy of ${source.id}` : isNew ? "new" : draft.id)}`
        : "Permission profiles"}</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="policy">§</span>
        <h2>Permission profiles</h2>
        <span class="cfg-count">${profiles.length}</span>
        <span class="cfg-head-actions">
          <a class="btn" href="#/config/profiles/new">+ New profile</a>
        </span>
      </div>
    </header>
    <div class="cfg-lede">How much this session may do on its own — a starting
      mode and three rule lists, under a name you can switch to in one action.
      Separate from a composition, which says which agent you are talking to and
      what tools it has; the same agent is worth running read-only while you
      review and unprompted while you run the tests.</div>
    <div class="pb-slot">${problemsCardHtml(data.problems, {
      title: "Profiles that are not what their file says",
      note: "Each of these is also marked on the profile itself in the list below.",
    })}</div>
    <span class="set-flash" data-page-flash></span>
    ${editing ? "" : `<div class="k-list">${
      profiles.map((p) => card(p, data.active, data.trusted)).join("")}</div>`}
    ${editing ? editorHtml(draft, { tools, scope, isNew, builtinIds }) : ""}
    ${editing && tools.length ? previewHtml(tools, draft.mode) : ""}
  </div>`;

  // The fresh inner node, not `host` — see the note above the wiring section.
  wireProblems(host.querySelector(".cfg-page-inner"), ctx);
  // The rail draws the same list with the same tick, and it is painted from the
  // snapshot the view cached when it opened. Handing it this answer keeps one
  // screen from showing two different active profiles.
  ctx.profiles = data;
  ctx.railDirty?.();
  if (editing) wireEditor(host, ctx, { tools });
  else wireList(host, ctx);
}

// ---- wiring ---------------------------------------------------------------
//
// Every listener goes on a node this render created, never on `host`: the page
// element outlives the render and re-rendering after a switch would otherwise
// leave the previous render's handler attached to it, so the second click on a
// long-lived page would fire two requests.

function wireList(host, ctx) {
  const inner = host.querySelector(".cfg-page-inner");
  // The flash is looked up after each repaint rather than captured, because the
  // repaint replaces the node it would have been written into.
  const say = (text, kind) =>
    flash(host.querySelector("[data-page-flash]"), text, kind);

  inner.addEventListener("click", async (e) => {
    const use = e.target.closest("[data-use]");
    const clear = e.target.closest("[data-clear]");
    const dup = e.target.closest("[data-duplicate]");
    const del = e.target.closest("[data-delete]");

    if (e.target.closest("[data-trust]")) {
      // The same grant the workspace banner takes, from the place the refusal
      // was read: a decision explained on one screen and taken on another is
      // one the user has to hold in their head across a navigation.
      try {
        await ctx.api.grantTrust();
        // Trust decides which tools exist, not only which profiles apply, so
        // the whole cached snapshot goes rather than this page's copy of it.
        ctx.invalidate?.();
        await renderProfiles(host, ctx, "");
        say("Trusted. Its profiles and its allow rules apply now.");
      } catch (err) {
        say(String(err.message).replace(/^\d+:\s*/, ""), "err");
      }
      return;
    }
    if (dup) {
      ctx.go(`#/config/profiles/new?from=${encodeURIComponent(dup.dataset.duplicate)}`);
      return;
    }
    if (use || clear) {
      const id = use ? use.dataset.use : "";
      const btn = use || clear;
      btn.disabled = true;
      try {
        const res = await ctx.api.setActiveProfile(id);
        // The server says which live sessions it reached; a switch that
        // silently did nothing to the conversation you are in is the failure
        // this line exists to rule out.
        const n = (res.applied_to || []).length;
        await renderProfiles(host, ctx, "");
        say(id
          ? `Now using “${id}”${n ? ` — applied to ${n} live session${n === 1 ? "" : "s"}` : ""}.`
          : "Cleared. The project's own rules apply on their own again.");
      } catch (err) {
        btn.disabled = false;
        say(String(err.message).replace(/^\d+:\s*/, ""), "err");
      }
      return;
    }
    if (del) {
      if (!window.confirm(
        `Delete the profile “${del.dataset.delete}” from ${del.dataset.scope} `
        + `settings? If it shadows a built-in of the same name, the built-in `
        + `comes back.`)) return;
      try {
        await ctx.api.deleteProfile(del.dataset.delete, del.dataset.scope);
        await renderProfiles(host, ctx, "");
      } catch (err) {
        say(String(err.message).replace(/^\d+:\s*/, ""), "err");
      }
    }
  });
}

function wireEditor(host, ctx, { tools }) {
  const inner = host.querySelector(".cfg-page-inner");
  const $ = (sel) => inner.querySelector(sel);
  const flashNode = $("[data-flash]");

  const readLists = () => {
    const out = {};
    for (const [key] of LISTS) out[key] = lines($(`[data-list="${key}"]`).value);
    return out;
  };

  const paintBad = (rules) => {
    for (const [key] of LISTS) {
      const bad = (rules[key] || []).filter((r) => !RULE_SHAPE.test(r));
      const node = $(`[data-bad="${key}"]`);
      node.textContent = bad.length
        ? `The engine can never match ${bad.join(", ")} — a rule is a tool name, `
          + `or a tool name with a pattern in brackets.`
        : "";
      node.classList.toggle("is-bad", !!bad.length);
    }
  };

  const verdict = $("[data-verdict]");
  const repaint = () => {
    const rules = readLists();
    paintBad(rules);
    if (!verdict) return;
    renderVerdict(verdict, {
      tools, rules,
      tool: $("#pf-try-tool").value,
      target: $("#pf-try-target").value,
      mode: $("#pf-try-mode").value,
    });
  };

  inner.addEventListener("input", (e) => {
    if (e.target.closest(".pf-lists, .pf-try")) repaint();
  });
  inner.addEventListener("change", (e) => {
    if (e.target.closest(".pf-try")) repaint();
    // The starting mode is the mode the preview asks about until you say
    // otherwise; two mode selectors that disagreed by default would be a
    // preview of a session nobody is going to run.
    if (e.target.id === "pf-mode" && $("#pf-try-mode")) {
      $("#pf-try-mode").value = e.target.value;
      repaint();
    }
  });
  repaint();

  $("[data-save]").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const body = {
      id: $("#pf-id").value.trim(),
      title: $("#pf-title").value.trim(),
      description: $("#pf-description").value.trim(),
      mode: $("#pf-mode").value,
      scope: $("#pf-scope").value,
      ...readLists(),
    };
    btn.disabled = true;
    try {
      await ctx.api.saveProfile(body);
    } catch (err) {
      const text = String(err.message).replace(/^\d+:\s*/, "");
      // 409 is the built-in shadow refusal and the only one worth re-offering:
      // the server spelled out what saving would do, so the confirmation is
      // about that sentence rather than about a generic "are you sure".
      if (/^409:/.test(err.message) || /built-in profile/.test(text)) {
        if (!window.confirm(`${text}\n\nWrite the copy anyway?`)) {
          btn.disabled = false;
          return;
        }
        try {
          await ctx.api.saveProfile({ ...body, shadow: true });
        } catch (err2) {
          btn.disabled = false;
          flash(flashNode, String(err2.message).replace(/^\d+:\s*/, ""), "err");
          return;
        }
      } else {
        btn.disabled = false;
        flash(flashNode, text, "err");
        return;
      }
    }
    // `go` repaints when the hash already matches and navigates when it does
    // not; either way the list is what comes back, freshly read.
    ctx.go("#/config/profiles");
  });
}
