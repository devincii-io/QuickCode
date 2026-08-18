// The command-tool panel, whose centre is the dry run.
//
// A command tool is one sentence of behaviour — *this argv array, with these
// holes filled in* — and every question anybody has about one ("what does it
// actually run", "does that value get re-split", "does the flag disappear when
// it is empty") is answered by seeing the resolved array. So the dry run is the
// panel, and the rest is context around it.
//
// **The dry run resolves; it does not execute.** There is no "run it once to
// check" button here, and the absence is deliberate: a command tool runs
// through the permission gate, where the approval prompt shows the exact argv
// before anything happens. A button on a configuration page that ran the
// command would be a second path to execution that skips that gate — the one
// path the whole design is built to keep single. To run it, ask an agent to,
// and approve it there.
//
// **The substitution is not implemented here.** `POST .../kernel/authored/
// dry-run` resolves the template with `kernel/authoring/argv.py` — literally
// the function `CommandTool.resolve_argv` calls — and this file renders the
// array it sends back. There is no local fallback, deliberately: a fallback
// only ever runs when the server disagrees or is unreachable, which is exactly
// when a second implementation would be believed and wrong. When the request
// fails the panel says the preview is unavailable and shows nothing.
//
// The four rules are still printed beside the panel. They are documentation of
// what the Python does, not a specification this file implements.

import { esc } from "../../util.js";
import { authToken, currentProject } from "../../api.js";
import { fencedJson, parseFrontmatter, patchFrontmatter } from "./scaffold.js";

// `api.js` owns the REST client, but the dry run is one route it does not
// expose, so the two lines of plumbing live here. The project scoping is the
// same rule its `P()` follows: a scoped route when a project is selected, the
// launch-directory route otherwise.
async function resolveArgv(body) {
  const pid = currentProject();
  const base = pid ? `/api/projects/${encodeURIComponent(pid)}` : "/api";
  const res = await fetch(`${base}/kernel/authored/dry-run`, {
    method: "POST",
    headers: { "x-quickcode-token": authToken(), "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

/** The form's reading of its own controls: a list textarea is one item per
 *  line, a checkbox is a real boolean. This is not substitution — it is what
 *  the fields *hold* — and the resolver on the other end decides what any of
 *  it means for the argv. */
function valuesForRequest(params, values) {
  const out = {};
  for (const p of params || []) {
    const raw = values[p.name];
    if (p.type === "list") {
      out[p.name] = Array.isArray(raw)
        ? raw
        : String(raw ?? "").split("\n").filter((line) => line.trim() !== "");
    } else if (p.type === "bool") {
      out[p.name] = raw === true || raw === "true";
    } else {
      out[p.name] = raw ?? "";
    }
  }
  return out;
}

const RULES = `<ul class="dr-rules">
  <li><code>{p}</code> inside an element substitutes in place:
    <code>--path={p}</code> stays one argument whatever the value contains. A
    value is never re-split on whitespace and never re-quoted.</li>
  <li>An element that is exactly <code>{p}</code> and resolves empty is
    <b>dropped</b> — that is how an optional argument disappears.</li>
  <li>A <code>list</code> parameter needs an element to itself; it expands to
    one argument per item.</li>
  <li>A <code>bool</code> parameter needs an element to itself; true emits its
    flag, false drops the element.</li>
</ul>`;

function fieldHtml(param, value) {
  const type = param.type || "string";
  const id = `dr-${param.name}`;
  const control = type === "bool"
    ? `<input type="checkbox" data-dr="${esc(param.name)}"${value === true || value === "true" ? " checked" : ""}>`
    : type === "list"
      ? `<textarea class="tp-input dr-list" rows="2" data-dr="${esc(param.name)}"
           placeholder="one per line">${esc(String(value ?? ""))}</textarea>`
      : type === "enum" && (param.choices || []).length
        ? `<select class="tp-input" data-dr="${esc(param.name)}">
             ${(param.choices || []).map((c) => `<option${
               String(value) === String(c) ? " selected" : ""}>${esc(c)}</option>`).join("")}
           </select>`
        : `<input class="tp-input" data-dr="${esc(param.name)}" spellcheck="false"
             value="${esc(String(value ?? ""))}"
             placeholder="${esc(param.default ? String(param.default) : type)}">`;
  return `<label class="dr-field" for="${esc(id)}">
    <span class="dr-name"><code>${esc(param.name)}</code>
      <span class="dr-type">${esc(type)}${param.required ? " · required" : ""}</span></span>
    ${control}
    ${param.description ? `<span class="dr-desc">${esc(param.description)}</span>` : ""}
  </label>`;
}

/** What the server sent, rendered. `res` is {loadable, argv, problems}. */
function argvHtml(res) {
  const errors = (res.problems || []).filter((p) => p.severity === "error");
  if (!res.loadable) {
    return `<div class="dr-unknown">${errors.length
      ? errors.map((p) => `${esc(p.message)}${p.fix ? ` <b>${esc(p.fix)}</b>` : ""}`)
          .join("<br>")
      : "The validator rejected this file, so there is no template to resolve."}
      <br>Refused at load time rather than substituted, so this file produces no
      tool until it is fixed.</div>`;
  }
  const argv = res.argv || [];
  if (!argv.length) {
    return `<div class="dr-empty">Every element resolved away. A template that
      can resolve to nothing runs nothing.</div>`;
  }
  return `<ol class="dr-argv">${argv.map((a, i) => `<li>
    <span class="dr-idx">${i}</span>
    <code class="dr-arg${a === "" ? " empty" : ""}">${a === "" ? "(empty string)" : esc(a)}</code>
  </li>`).join("")}</ol>
  <div class="dr-line"><code>${esc(argv.join(" "))}</code></div>`;
}

/** The one thing shown when the resolver cannot be reached. Guessing at the
 *  array here is the bug this panel exists to avoid, so it guesses nothing. */
function unavailableHtml(err) {
  return `<div class="dr-unknown">The dry run is unavailable: ${esc(err.message)}.
    <br>The array is resolved by the same code that runs it, so there is nothing
    to show while that answer is missing — a locally reconstructed one would
    agree with what actually runs only by luck.</div>`;
}

const DEBOUNCE_MS = 150;

// One dry run is on screen at a time, so the in-flight request and its debounce
// are module state: the editor rebuilds this panel on *every* keystroke, and a
// timer scoped to one mount would debounce nothing across the rebuilds.
let seq = 0;
let timer = 0;
// The last array the server sent, so those rebuilds do not blink through
// "Resolving…" while the answer already on screen is still the true one. Keyed
// by the template it describes — a different tool, or an edited argv line,
// starts from the placeholder rather than showing the previous tool's command.
let lastKey = "";
let lastHtml = "";

const PENDING = `<div class="dr-empty">Resolving…</div>`;

function keyOf(template, params) {
  return JSON.stringify([template || [], (params || []).map((p) => [p.name, p.type])]);
}

/** The dry run on its own — used by the editor panel and by a tool's card.
 *  The output slot is filled by `wireDryRun`: the array comes from the server
 *  and this function is synchronous. */
export function dryRunHtml(template, params, values, { lede = "" } = {}) {
  const known = keyOf(template, params) === lastKey && lastHtml;
  return `<section class="wb-sec dr">
    <h4>Dry run <span class="wb-count">${(template || []).length} elements</span></h4>
    ${lede ? `<p class="wb-note block">${lede}</p>` : ""}
    ${(params || []).length
      ? `<div class="dr-fields">${(params || []).map(
          (p) => fieldHtml(p, values[p.name])).join("")}</div>`
      : `<p class="wb-note block">No parameters — this tool always runs the same
          command.</p>`}
    <div class="dr-out" data-dr-out>${known ? lastHtml : PENDING}</div>
    <p class="wb-note block">Resolved by the server, not executed — the same
      <code>argv.py</code> the runtime calls, so this array cannot disagree with
      the one that runs. To run it, ask an agent to call it: the approval prompt
      shows this same array before anything starts, and that is the only path to
      running it. A configuration page that could run a command would be a
      second path around the permission gate.</p>
    ${RULES}
  </section>`;
}

/** Re-resolve as the fields are typed in, without repainting the fields
 *  themselves — otherwise the caret jumps on every keystroke.
 *
 *  `source` (when given) is a getter for the file as it currently stands in the
 *  editor, unsaved included: sending the text means the problems come back from
 *  the validator the runtime uses rather than from a second reading of the file
 *  here. Without it the already-validated template is sent as it is. Either way
 *  the substitution happens once, on the server. */
export function wireDryRun(host, template, params, values, { source = null } = {}) {
  const out = host.querySelector("[data-dr-out]");
  if (!out) return;
  const key = keyOf(template, params);

  const body = () => (source
    ? { text: source(), values: valuesForRequest(params, values) }
    : { argv: template || [], params: params || [],
        values: valuesForRequest(params, values) });

  const resolve = async () => {
    const mine = ++seq;
    let html;
    try {
      html = argvHtml(await resolveArgv(body()));
    } catch (err) {
      html = unavailableHtml(err);
    }
    // A stale answer must never land on top of a fresher one.
    if (mine !== seq) return;
    lastKey = key;
    lastHtml = html;
    if (out.isConnected) out.innerHTML = html;
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(resolve, DEBOUNCE_MS);
  };

  for (const el of host.querySelectorAll("[data-dr]")) {
    const name = el.dataset.dr;
    const read = () => (el.type === "checkbox" ? el.checked : el.value);
    const changed = () => { values[name] = read(); schedule(); };
    el.addEventListener("input", changed);
    el.addEventListener("change", changed);
  }
  schedule();
}

// ---- the editor's side panel ---------------------------------------------

export function toolPanel({ read, write }) {
  const values = {};                    // survives a repaint of the panel

  const parse = () => {
    const text = read();
    const { meta } = parseFrontmatter(text);
    const argv = fencedJson(text, "argv");
    const params = fencedJson(text, "params");
    return { meta, argv, params };
  };

  return {
    html() {
      const { meta, argv, params } = parse();
      const template = Array.isArray(argv.value) ? argv.value : [];
      const declared = Array.isArray(params.value) ? params.value : [];
      for (const p of declared) {
        if (!(p.name in values)) values[p.name] = p.default ?? (p.type === "bool" ? false : "");
      }
      const broken = [
        argv.found ? "" : "There is no <code>```json argv</code> block, so this file declares no command.",
        argv.error ? `The <code>argv</code> block is not valid JSON: ${esc(argv.error)}` : "",
        params.error ? `The <code>params</code> block is not valid JSON: ${esc(params.error)}` : "",
      ].filter(Boolean);

      return `<div class="ed-side-inner">
        <section class="wb-sec">
          <h4>What the model is told</h4>
          <dl class="wb-rows">
            <dt>name</dt><dd><code>${esc(meta.name || "—")}</code>
              <span class="wb-note">this is the name it calls</span></dd>
            <dt>description</dt><dd>${esc(meta.description || "—")}</dd>
            <dt>parameters</dt><dd>${declared.length
              ? declared.map((p) => `<code class="wb-pattern">${esc(p.name)}</code>`).join("")
              : `<span class="wb-note">none</span>`}</dd>
            <dt>permission</dt><dd><span class="wb-note">always mutating: every
              call stops at the gate, and the prompt shows the resolved argv.
              A <code>read_only: true</code> claim is reported, never
              honoured.</span></dd>
          </dl>
          <label class="ed-field"><span>title</span>
            <input class="tp-input" data-fm="title" value="${esc(meta.title || "")}"></label>
          <p class="wb-note block">Editing a field here rewrites exactly that
            frontmatter line and leaves the rest of the file alone, so switching
            between this and the text is free in both directions.</p>
        </section>
        ${broken.length
          ? `<section class="wb-sec"><h4>Dry run</h4>
              <div class="dr-unknown">${broken.join("<br>")}</div></section>`
          : dryRunHtml(template, declared, values)}
      </div>`;
    },
    mount(node) {
      const { argv, params } = parse();
      if (Array.isArray(argv.value)) {
        // `source` sends the file as typed, unsaved and all: the fences are
        // parsed here only to build the *fields*, while what the argv means is
        // decided once, by the validator and `argv.py` on the other end.
        wireDryRun(node, argv.value,
                   Array.isArray(params.value) ? params.value : [], values,
                   { source: read });
      }
      for (const el of node.querySelectorAll("[data-fm]")) {
        el.addEventListener("change", () => {
          write(patchFrontmatter(read(), el.dataset.fm, el.value));
        });
      }
    },
  };
}
