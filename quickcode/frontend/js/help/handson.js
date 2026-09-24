// Help ▸ Hands-on — three widgets that teach by doing.
//
// The rule every one of them obeys: **compute the answer, never illustrate it.**
// A screenshot of a decision teaches you what a decision looks like; a thing
// that actually decides teaches you the decision.
//
// Where they get their answers, stated here once and again in the UI beside
// each one:
//
//   Rule sandbox    live. Every keystroke (debounced) asks this project's real
//                   permission engine through POST …/permissions/explain and
//                   draws the trace it returns (js/help/explain.js). The rules
//                   typed here are added for that one question and written
//                   nowhere.
//   Mode comparison live. The tools are this install's, and the withholding
//                   rule is the one PlanModeHook applies.
//   Layering        live. It calls the real resolver over HTTP and shows the
//                   provenance chain it returns. Nothing is recomputed here.

import { esc } from "../util.js";
import { MODES, MODE_IDS } from "./modes.js";
import { explainErrorHtml, explainHtml, explainer } from "./explain.js";
import { getFacts } from "./view.js";
import { honesty, link, note, pageHtml, sub } from "./ui.js";

// ---------------------------------------------------------------------------
// 1. The permission-rule sandbox
// ---------------------------------------------------------------------------

// Each sample teaches exactly one thing, and every one of them produces a
// verdict people get wrong on their first guess. They leave the project's own
// rules out, so the lesson is the same in every project.
const SAMPLES = [
  {
    label: "deny beats allow",
    mode: "ask", tool: "bash", target: "git push origin main",
    allow: "bash(git **)", ask: "", deny: "bash(git push**)",
  },
  {
    label: "one bad clause gates the line",
    mode: "auto-edit", tool: "bash", target: "npm test && rm -rf build",
    allow: "bash(npm *)", ask: "", deny: "",
  },
  {
    label: "protected path beats an allow",
    mode: "auto-edit", tool: "edit", target: ".env",
    allow: "edit(**)", ask: "", deny: "",
  },
  {
    label: "a deny beats the protected-path prompt",
    mode: "ask", tool: "read", target: ".env",
    allow: "read", ask: "", deny: "read(**.env)",
  },
  {
    label: "read-only builtins in plan mode",
    mode: "plan", tool: "bash", target: "ls -la src",
    allow: "", ask: "", deny: "",
  },
  {
    label: "a redirection disqualifies every allow",
    mode: "ask", tool: "bash", target: "echo done > notes.txt",
    allow: "bash(echo **)", ask: "", deny: "",
  },
  {
    label: "a command inside a command",
    mode: "ask", tool: "bash", target: "find . -name '*.log' -exec rm {} +",
    allow: "bash(find **)", ask: "", deny: "bash(rm **)",
  },
  {
    label: "* stops at a slash",
    mode: "ask", tool: "edit", target: "src/app/main.py",
    allow: "edit(src/*)", ask: "", deny: "",
  },
];

/** Live tools, as {name, character}. `metadata.character` is derived from the
 *  tool's real PermissionSpec by the kernel, so this is the tool's own
 *  declaration rather than a table maintained here. */
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

function lines(text) {
  return String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
}

/** The declared shape the engine answered with, in words. */
function shapeText(spec) {
  if (!spec) return "";
  if (spec.shell) return "shell: the target is a command line, judged per subcommand";
  const what = spec.mutates ? "mutating" : "read-only";
  const target = spec.target_field
    ? `, target ${spec.target_field}${spec.path_target ? " (a path)" : ""}` : ", no target";
  return `${what}${target}${spec.executes ? ", runs a program" : ""}`;
}

function sandboxHtml(tools) {
  const opt = (t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`;
  return `<section class="hp-widget" id="hp-sandbox">
    <div class="hp-widget-head">
      <h4>Permission sandbox</h4>
      <span class="hp-widget-kicker">ask the real gate why</span>
    </div>
    <p class="hp-widget-lede">Pick a tool, a mode and what it would act on, add
      whatever rules you like, and see the answer <em>and the reason</em> — from
      this project's own permission engine, the code that gates every tool call.
      The trace underneath shows which check answered and which never got to run,
      which is usually the part that was surprising.</p>

    <div class="hp-samples">
      <span class="hp-samples-label">Try</span>
      ${SAMPLES.map((s, i) => `<button class="hp-sample" type="button"
        data-sample="${i}">${esc(s.label)}</button>`).join("")}
    </div>

    <div class="hp-fields">
      <div class="hp-field">
        <label for="hp-sb-mode">Mode</label>
        <select id="hp-sb-mode">
          ${MODE_IDS.map((m) => `<option value="${esc(m)}"${
            m === "ask" ? " selected" : ""}>${esc(m)}</option>`).join("")}
        </select>
      </div>
      <div class="hp-field">
        <label for="hp-sb-tool">Tool</label>
        <select id="hp-sb-tool">${tools.map(opt).join("")}</select>
        <span class="hp-field-help" id="hp-sb-shape"></span>
      </div>
      <div class="hp-field">
        <label for="hp-sb-target">What it would act on</label>
        <input id="hp-sb-target" spellcheck="false"
               placeholder="src/app.py — or a whole command line">
        <span class="hp-field-help">The argument the tool declares as its
          target.</span>
      </div>
    </div>

    <div class="hp-fields">
      <div class="hp-field">
        <label for="hp-sb-deny">deny rules</label>
        <textarea id="hp-sb-deny" rows="2" spellcheck="false"
                  placeholder="one per line"></textarea>
      </div>
      <div class="hp-field">
        <label for="hp-sb-ask">ask rules</label>
        <textarea id="hp-sb-ask" rows="2" spellcheck="false"
                  placeholder="one per line"></textarea>
      </div>
      <div class="hp-field">
        <label for="hp-sb-allow">allow rules</label>
        <textarea id="hp-sb-allow" rows="2" spellcheck="false"
                  placeholder="one per line"></textarea>
      </div>
    </div>
    <label class="hp-check">
      <input type="checkbox" id="hp-sb-project" checked>
      Start from this project's own rules and active profile — untick to see
      the rules above on their own
    </label>

    <div id="hp-sb-out" aria-live="polite"></div>

    ${honesty("live", "Live: every answer here comes from this project's "
      + "running permission engine (POST …/permissions/explain) — the same "
      + "code, rule matching and path resolution that gates a real tool call. "
      + "The rules you type are added for the one question and written nowhere. "
      + "Command hooks, which can only tighten an answer, are not run.")}
  </section>`;
}

function wireSandbox(root, tools, api) {
  const $ = (id) => root.querySelector(id);
  const mode = $("#hp-sb-mode");
  const tool = $("#hp-sb-tool");
  const target = $("#hp-sb-target");
  const shape = $("#hp-sb-shape");
  const deny = $("#hp-sb-deny");
  const ask = $("#hp-sb-ask");
  const allow = $("#hp-sb-allow");
  const project = $("#hp-sb-project");
  const out = $("#hp-sb-out");
  if (!mode || !tool || !out) return;

  const paint = (result, error) => {
    if (error) {
      out.innerHTML = explainErrorHtml(error);
      shape.textContent = "";
      return;
    }
    out.innerHTML = explainHtml(result);
    shape.textContent = shapeText(result.spec);
  };
  const ask$ = explainer((body) => api.explainPermission(body), paint);

  const run = () => ask$({
    tool: tool.value,
    target: target.value,
    mode: mode.value,
    rules: { allow: lines(allow.value), ask: lines(ask.value), deny: lines(deny.value) },
    project_rules: project.checked,
    profile: project.checked,
  });

  for (const node of [mode, tool, target, deny, ask, allow, project]) {
    node.addEventListener("input", run);
    node.addEventListener("change", run);
  }

  const load = (s) => {
    mode.value = s.mode;
    // A sample naming a tool this install does not have keeps whatever is
    // selected rather than silently evaluating a different call.
    if (tools.some((t) => t.name === s.tool)) tool.value = s.tool;
    target.value = s.target;
    allow.value = s.allow;
    ask.value = s.ask;
    deny.value = s.deny;
    project.checked = false;
    run();
  };

  root.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-sample]");
    const s = btn && SAMPLES[Number(btn.dataset.sample)];
    if (s) load(s);
  });

  // Open on the first sample, so the widget says something before it is touched.
  load(SAMPLES[0]);
}

// ---------------------------------------------------------------------------
// 2. Mode comparison — which tools each mode even offers
// ---------------------------------------------------------------------------

// The `metadata.character` values of tools that plan mode keeps: the shell,
// and the ones that do not mutate. The kernel derives the character from the
// tool's PermissionSpec; an unknown one is treated as mutating, which is the
// engine's own fallback for an undeclared tool.
const KEPT_IN_PLAN = new Set(["shell", "file_read", "read_only", "internal_write"]);

function offeredIn(mode, tool) {
  // PlanModeHook.visible_tools, exactly: the plan tool is offered only in plan
  // mode, and in plan mode a tool that mutates and is not a shell tool is
  // withheld from the request entirely.
  if (tool.name === "plan") return mode === "plan";
  return mode !== "plan" || KEPT_IN_PLAN.has(tool.character);
}

function modeCompareHtml(tools) {
  return `<section class="hp-widget" id="hp-modes">
    <div class="hp-widget-head">
      <h4>What each mode offers</h4>
      <span class="hp-widget-kicker">this install's tools</span>
    </div>
    <p class="hp-widget-lede">Only one mode changes which tools the model is
      <em>shown</em>. The rest change the default <em>answer</em>. Pick a mode to
      see both.</p>
    <div class="hp-toggle-row" role="group" aria-label="Permission mode">
      ${MODES.map((m, i) => `<button class="hp-toggle" type="button"
        data-mode="${esc(m.id)}" aria-pressed="${i === 1}">${esc(m.id)}</button>`).join("")}
    </div>
    <div id="hp-modes-out" aria-live="polite"></div>
    ${honesty("live", `Live: the ${tools.length} tools listed are the ones this
      install registered, and the withholding rule is the one the plan-mode hook
      applies. The default answers are the engine's.`)}
  </section>`;
}

function modeBodyHtml(modeId, tools) {
  const m = MODES.find((x) => x.id === modeId);
  if (!m) return "";
  const offered = tools.filter((t) => offeredIn(modeId, t));
  const withheld = tools.filter((t) => !offeredIn(modeId, t));
  return `<p class="hp-p">${esc(m.what)}</p>
    <div class="hp-table-wrap">
      <table class="hp-table">
        <thead><tr><th>Kind of call</th><th>Default answer in <code>${esc(m.id)}</code></th></tr></thead>
        <tbody>
          <tr><th>a mutating tool</th><td>${esc(m.write)}</td></tr>
          <tr><th>a read-only tool</th><td>${esc(m.read)}</td></tr>
          <tr><th>a shell command</th><td>${esc(m.shell)}</td></tr>
          <tr><th>a protected path</th><td>${esc(m.protected)}</td></tr>
        </tbody>
      </table>
    </div>
    <div class="hp-tools-head">Offered to the model (${offered.length})</div>
    <div class="hp-tools">${offered.map((t) =>
      `<span class="hp-tool">${esc(t.name)}</span>`).join("")}</div>
    ${withheld.length ? `
      <div class="hp-tools-head">Withheld (${withheld.length})</div>
      <div class="hp-tools">${withheld.map((t) =>
        `<span class="hp-tool" data-state="withheld">${esc(t.name)}</span>`).join("")}</div>
      <p class="hp-p hp-tools-gap">These are not denied per call — they
        are left out of the request. A tool the model can see is a tool it will
        try, and denying every attempt would spend a round on each one and teach
        it that the mode is only advice.</p>` : ""}
    ${m.caveat ? `<p class="hp-honesty">${esc(m.caveat)}</p>` : ""}`;
}

function wireModes(root, tools) {
  const out = root.querySelector("#hp-modes-out");
  if (!out) return;
  const show = (id) => {
    out.innerHTML = modeBodyHtml(id, tools);
    root.querySelectorAll("[data-mode]").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.mode === id)));
  };
  root.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (b) show(b.dataset.mode);
  });
  show("ask");
}

// ---------------------------------------------------------------------------
// 3. Composition layering — the real resolver, over HTTP
// ---------------------------------------------------------------------------

const LAYER_NOTE = {
  default: "the dataclass defaults and the enabled plugin pool",
  user: "~/.quickcode/settings.json",
  project: "<project>/.quickcode/settings.json",
  preset: "the composition and its bindings",
  agent: "the agent definition's own composition",
  session: "recorded in the session's meta record when it opened",
  call: "the agent tool's own arguments at spawn time",
  parent: "the spawning agent — a child can never be wider",
  runtime: "depth limits and delegation-by-depth",
};

// The keys worth showing. `chain` carries one entry per dotted path, and the
// interesting ones are the values with a layering story rather than the
// hundreds of per-tool entries.
const CHAIN_KEYS = [
  ["model", "which model answers"],
  ["ceiling", "the most privileged mode this agent may ever reach"],
  ["max_turns", "the delegation budget"],
];

function layeringHtml() {
  return `<section class="hp-widget" id="hp-layers">
    <div class="hp-widget-head">
      <h4>Where a value came from</h4>
      <span class="hp-widget-kicker">the real resolver, asked live</span>
    </div>
    <p class="hp-widget-lede">An agent's configuration is assembled from layers,
      and two kinds of field combine in two different ways. Pick an agent and a
      composition; this asks the backend to resolve it for real and shows the
      provenance it hands back.</p>
    <div class="hp-fields">
      <div class="hp-field">
        <label for="hp-ly-agent">Agent</label>
        <select id="hp-ly-agent"></select>
      </div>
      <div class="hp-field">
        <label for="hp-ly-preset">Composition</label>
        <select id="hp-ly-preset"></select>
      </div>
    </div>
    <div id="hp-ly-out" aria-live="polite"></div>
    ${honesty("live", "Live: this calls the same resolver endpoint the agent "
      + "workbench uses and renders the provenance chain it returns. Nothing "
      + "about the layering is recomputed in the browser.")}
  </section>`;
}

function chainRowsHtml(resolved) {
  const chain = resolved?.chain || {};
  const value = {
    model: resolved?.model || "(the install default)",
    ceiling: resolved?.ceiling || "",
    max_turns: resolved?.max_turns != null ? String(resolved.max_turns) : "",
  };
  return CHAIN_KEYS.map(([key, what]) => {
    const entries = chain[key] || [];
    return `<div class="hp-chain">
      <div class="hp-chain-head"><code>${esc(key)}</code>
        <span class="hp-dim-inline">${esc(what)}</span></div>
      ${entries.length ? `<div class="hp-layers">
        ${entries.map((p, i) => `<div class="hp-layer"
            data-role="${i === entries.length - 1 ? "win" : ""}">
          <span class="hp-layer-n">${i + 1}</span>
          <span class="hp-layer-name">${esc(p.layer || "?")}</span>
          <span class="hp-layer-value">${esc(p.source || "")}${
            p.rule ? ` <code>${esc(p.rule)}</code>` : ""}${
            p.note ? ` — ${esc(p.note)}` : ""}</span>
          <span class="hp-layer-tag">${i === entries.length - 1 ? "wins" : ""}</span>
        </div>`).join("")}
      </div>` : `<p class="hp-honesty">No layer stated this, so it kept the
        default. That is a real answer, not a missing one.</p>`}
      <div class="hp-layer-out">
        <span class="hp-layer-n">=</span>
        <span class="hp-layer-name">result</span>
        <span class="hp-layer-value"><code>${esc(value[key] || "—")}</code></span>
        <span class="hp-layer-tag"></span>
      </div>
    </div>`;
  }).join("");
}

function capabilityHtml(resolved) {
  const tools = resolved?.tools || [];
  const denied = resolved?.denied_tools || [];
  return `<div class="hp-chain">
    <div class="hp-chain-head"><code>tools</code>
      <span class="hp-dim-inline">a capability field — layers
      <strong>intersect</strong></span></div>
    <p class="hp-p"><strong>${esc(String(tools.length))}</strong> granted,
      <strong>${esc(String(denied.length))}</strong> in the pool and not granted.
      Because capability fields intersect, “which layer wins” is not a question
      that can be asked about them — no layer, and no argument at spawn time, can
      widen what a lower one allowed. That is the whole reason a subagent cannot
      escalate past its parent.</p>
    ${denied.length ? `<div class="hp-tools">${denied.slice(0, 24).map((t) =>
      `<span class="hp-tool" data-state="withheld">${esc(t)}</span>`).join("")}${
      denied.length > 24 ? `<span class="hp-tool">+${denied.length - 24} more</span>` : ""}
    </div>` : ""}
  </div>`;
}

function layerLegendHtml() {
  return `<div class="hp-chain">
    <div class="hp-chain-head">The layers, in order</div>
    <div class="hp-layers">
      ${Object.entries(LAYER_NOTE).map(([name, note2], i) => `
        <div class="hp-layer" data-role="silent">
          <span class="hp-layer-n">${i}</span>
          <span class="hp-layer-name">${esc(name)}</span>
          <span class="hp-layer-value">${esc(note2)}</span>
          <span class="hp-layer-tag"></span>
        </div>`).join("")}
    </div>
  </div>`;
}

function wireLayering(root, facts) {
  const agentSel = root.querySelector("#hp-ly-agent");
  const presetSel = root.querySelector("#hp-ly-preset");
  const out = root.querySelector("#hp-ly-out");
  if (!agentSel || !presetSel || !out) return;

  const agents = facts.agents?.agents || [];
  const presets = facts.presets?.presets || [];
  if (!agents.length) {
    out.innerHTML = `<div class="hp-degraded">The agent inventory could not be
      read, so there is nothing to resolve. The layer table below is still
      accurate — it is declared in kernel/resolve.py.</div>${layerLegendHtml()}`;
    return;
  }
  agentSel.innerHTML = agents.map((a) =>
    `<option value="${esc(a.id)}">${esc(a.title || a.id)}</option>`).join("");
  presetSel.innerHTML = presets.map((p) =>
    `<option value="${esc(p.id)}"${p.id === facts.presets?.active ? " selected" : ""}
      >${esc(p.title || p.id)}</option>`).join("")
    || `<option value="">the active composition</option>`;

  let inflight = 0;
  const run = async () => {
    const ticket = ++inflight;
    out.innerHTML = `<div class="hp-loading">Resolving…</div>`;
    let data;
    try {
      data = await facts.api.resolvedAgent(agentSel.value,
        { preset: presetSel.value || "" });
    } catch (err) {
      if (ticket !== inflight) return;
      out.innerHTML = `<div class="hp-degraded">The resolver refused that
        combination: ${esc(err.message)}. Nothing is shown rather than
        something invented.</div>${layerLegendHtml()}`;
      return;
    }
    if (ticket !== inflight) return;   // a newer selection is already in flight
    const resolved = data.resolved || {};
    out.innerHTML = `
      <p class="hp-p"><strong>Value fields</strong> combine by last writer wins,
        down the ordered layer list.</p>
      ${chainRowsHtml(resolved)}
      <p class="hp-p"><strong>Capability fields</strong> combine by intersection,
        which is why no layer can widen a child.</p>
      ${capabilityHtml(resolved)}
      ${layerLegendHtml()}`;
  };

  agentSel.addEventListener("change", run);
  presetSel.addEventListener("change", run);
  run();
}

// ---------------------------------------------------------------------------
// the page
// ---------------------------------------------------------------------------

export async function renderHandsOn(host) {
  host.innerHTML = pageHtml("Hands-on", {
    crumb: "Help",
    sigil: "fn",
    lede: `Three things you can poke at without spending a token or touching a
      file. Each one says underneath whether it asked the backend or worked the
      answer out in your browser — because a widget that quietly reimplements the
      real thing is how a help page starts being wrong.`,
    body: `
      ${sub("1 · Will this be allowed?")}
      <div id="hp-slot-sandbox"><div class="hp-loading">Reading the tool
        registry…</div></div>

      ${sub("2 · What changes when you change the mode?")}
      <div id="hp-slot-modes"><div class="hp-loading">Reading the tool
        registry…</div></div>

      ${sub("3 · Why does this agent have that value?")}
      <div id="hp-slot-layers"><div class="hp-loading">Reading the agent
        inventory…</div></div>

      ${note("None of this touches your project", `
        <p class="hp-p">Nothing on this page writes a file, changes a setting or
          sends anything to a model. The sandbox reads your project's rules to
          answer, and the rules you type there exist for that one question — try
          them, then go and write the ones you want in
          ${link("#/config/profiles", "Permission profiles")} or
          <code>.quickcode/settings.json</code>. From a terminal,
          <code>qc why "&lt;command&gt;"</code> asks the same engine.</p>`)}
    `,
  });

  const facts = await getFacts();
  const sandboxSlot = host.querySelector("#hp-slot-sandbox");
  const modesSlot = host.querySelector("#hp-slot-modes");
  const layersSlot = host.querySelector("#hp-slot-layers");
  if (!sandboxSlot) return;

  const tools = toolChoices(facts?.kernel);

  if (!tools.length) {
    const msg = `<div class="hp-degraded">The tool registry could not be read, so
      these two widgets are not shown. They evaluate against this install's real
      tools and their real declared shapes, and a demo against invented ones
      would teach the wrong thing.</div>`;
    sandboxSlot.innerHTML = msg;
    if (modesSlot) modesSlot.innerHTML = msg;
  } else {
    sandboxSlot.innerHTML = sandboxHtml(tools);
    wireSandbox(sandboxSlot, tools, facts.api);
    if (modesSlot) {
      modesSlot.innerHTML = modeCompareHtml(tools);
      wireModes(modesSlot, tools);
    }
  }

  if (layersSlot) {
    if (!facts) {
      layersSlot.innerHTML = `<div class="hp-degraded">The backend could not be
        reached, so nothing is resolved here.</div>`;
    } else {
      layersSlot.innerHTML = layeringHtml();
      wireLayering(layersSlot, facts);
    }
  }
}
