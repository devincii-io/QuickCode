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
// The substitution below mirrors `kernel/authoring/argv.py` rule for rule.
// Being a second implementation it can drift, so it is labelled a preview and
// the four rules are printed next to it; the authority is the argv the
// permission prompt shows, which comes from the Python.

import { esc } from "../../util.js";
import { fencedJson, parseFrontmatter, patchFrontmatter } from "./scaffold.js";

const TOKEN = () => /\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

function placeholders(element) {
  const out = [];
  const re = TOKEN();
  let m;
  while ((m = re.exec(element)) !== null) if (m[1] !== undefined) out.push(m[1]);
  return out;
}

function wholePlaceholder(element) {
  const names = placeholders(element);
  return names.length === 1 && element === `{${names[0]}}` ? names[0] : "";
}

function scalar(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function renderElement(element, values) {
  const out = [];
  const re = TOKEN();
  let cursor = 0;
  let m;
  while ((m = re.exec(element)) !== null) {
    out.push(element.slice(cursor, m.index));
    if (m[1] === undefined) out.push(m[0] === "{{" ? "{" : "}");
    else out.push(scalar(values[m[1]]));
    cursor = m.index + m[0].length;
  }
  out.push(element.slice(cursor));
  return out.join("");
}

/** `argv.py:render_argv`, in the browser. `params` is {name: {type, flag}}. */
export function renderArgv(template, params, values) {
  const out = [];
  for (const element of template || []) {
    const name = wholePlaceholder(element);
    if (name && params[name]) {
      const type = params[name].type || "string";
      const value = values[name];
      if (type === "list") {
        const items = Array.isArray(value)
          ? value : String(value ?? "").split("\n");
        for (const item of items) if (scalar(item).trim() !== "") out.push(scalar(item));
        continue;
      }
      if (type === "bool") {
        if (value === true || value === "true") {
          out.push(params[name].flag || `--${name.replace(/_/g, "-")}`);
        }
        continue;
      }
      const text = scalar(value);
      if (text === "") continue;             // rule 3: the whole element drops
      out.push(text);
      continue;
    }
    out.push(renderElement(element, values));
  }
  return out;
}

/** Every `{name}` the template mentions that no parameter declares. The
 *  validator refuses these outright, so a dry run showing one has to say the
 *  file will not load rather than quietly substituting an empty string. */
export function unknownNames(template, params) {
  const seen = new Set();
  for (const element of template || []) {
    for (const name of placeholders(element)) if (!params[name]) seen.add(name);
  }
  return [...seen];
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

function argvHtml(template, paramsByName, values) {
  const unknown = unknownNames(template, paramsByName);
  if (unknown.length) {
    return `<div class="dr-unknown">The template mentions
      ${unknown.map((n) => `<code>{${esc(n)}}</code>`).join(", ")}, which
      ${unknown.length === 1 ? "is not a declared parameter" : "are not declared parameters"}.
      That is refused at load time rather than substituted empty, so this file
      will not produce a tool until the names match.</div>`;
  }
  const argv = renderArgv(template, paramsByName, values);
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

/** The dry run on its own — used by the editor panel and by a tool's card. */
export function dryRunHtml(template, params, values, { lede = "" } = {}) {
  const byName = Object.fromEntries((params || []).map((p) => [p.name, p]));
  return `<section class="wb-sec dr">
    <h4>Dry run <span class="wb-count">${(template || []).length} elements</span></h4>
    ${lede ? `<p class="wb-note block">${lede}</p>` : ""}
    ${(params || []).length
      ? `<div class="dr-fields">${(params || []).map(
          (p) => fieldHtml(p, values[p.name])).join("")}</div>`
      : `<p class="wb-note block">No parameters — this tool always runs the same
          command.</p>`}
    <div class="dr-out" data-dr-out>${argvHtml(template, byName, values)}</div>
    <p class="wb-note block">Resolved, not executed. To run it, ask an agent to
      call it: the approval prompt shows this same array before anything starts,
      and that is the only path to running it. A configuration page that could
      run a command would be a second path around the permission gate.</p>
    ${RULES}
  </section>`;
}

/** Re-resolve in place as the fields are typed in, without repainting the
 *  fields themselves — otherwise the caret jumps on every keystroke. */
export function wireDryRun(host, template, params, values) {
  const byName = Object.fromEntries((params || []).map((p) => [p.name, p]));
  const out = host.querySelector("[data-dr-out]");
  if (!out) return;
  const repaint = () => { out.innerHTML = argvHtml(template, byName, values); };
  for (const el of host.querySelectorAll("[data-dr]")) {
    const name = el.dataset.dr;
    const read = () => (el.type === "checkbox" ? el.checked : el.value);
    el.addEventListener("input", () => { values[name] = read(); repaint(); });
    el.addEventListener("change", () => { values[name] = read(); repaint(); });
  }
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
        wireDryRun(node, argv.value,
                   Array.isArray(params.value) ? params.value : [], values);
      }
      for (const el of node.querySelectorAll("[data-fm]")) {
        el.addEventListener("change", () => {
          write(patchFrontmatter(read(), el.dataset.fm, el.value));
        });
      }
    },
  };
}
