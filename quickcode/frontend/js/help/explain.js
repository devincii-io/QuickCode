// "Why would this be allowed?" — drawing the permission engine's own answer.
//
// The Help sandbox and the profile editor's preview both ask the running
// engine (POST …/permissions/explain, server/permissions_api.py) and render
// what comes back here. There is deliberately no rule logic in this file: the
// decision, the order of the checks and every sentence in the trace are the
// backend's (core/permission_explain.py), so the page cannot disagree with the
// gate it describes. This replaced js/help/engine.js, a line-by-line port of
// core/permissions.py that had already drifted from it.

import { esc } from "../util.js";

const OUTCOME = { allow: "runs without asking", ask: "asks you", deny: "is refused" };

// Presentation names for the engine's step ids. A step this table does not
// know is shown by its id rather than hidden.
const STEP_LABEL = {
  protected_path: "protected path",
  plan_mode: "plan mode",
  deny_rule: "deny rule",
  ask_rule: "ask rule",
  allow_rule: "allow rule",
  read_only: "read-only tool",
  mode_default: "mode default",
  shell: "decomposition",
  parsed: "parsed",
  readonly_builtin: "read-only builtin",
  circuit_breaker: "circuit breaker",
  most_restrictive: "most restrictive",
};

const SOURCE_LABEL = {
  "new session": "a new session's mode",
  session: "this session's mode",
  request: "the mode asked about",
};

function stepHtml(step) {
  const decided = !!step.decision;
  const label = step.step === "subcommand"
    ? step.command : (STEP_LABEL[step.step] || step.step);
  const inner = step.step === "subcommand" && (step.steps || []).length
    ? `<ol class="hp-trace hp-trace-sub">${step.steps.map(stepHtml).join("")}</ol>` : "";
  return `<li data-hit="${decided ? "1" : "skip"}">
      <span class="hp-trace-mark">${decided ? "▸" : "·"}</span>
      <span class="hp-trace-step">${esc(label)}</span>
      <div class="hp-trace-why">${decided
        ? `<b class="hp-trace-decision" data-outcome="${esc(step.decision)}">${
          esc(step.decision)}</b> ` : ""}${step.step === "subcommand" ? "" : esc(step.why || "")}${
        inner}</div>
    </li>`;
}

function postureText(posture) {
  const bits = [`${posture.mode} mode (${SOURCE_LABEL[posture.mode_source] || posture.mode_source})`];
  bits.push(posture.profile ? `profile ${posture.profile}` : "no profile");
  if (posture.project_rules === false) bits.push("project rules left out");
  bits.push(posture.trusted ? "project trusted" : "project not trusted");
  return bits.join(" · ");
}

/** The whole answer: verdict, why, what "Always allow" would do, the things
 *  outside the gate that change it, and the ordered trace. */
export function explainHtml(p) {
  const s = p.suggestion;
  const invalid = p.invalid_rules || [];
  return `<div class="hp-verdict" data-outcome="${esc(p.decision)}">
      <span class="hp-verdict-badge">${esc(p.decision)}</span>
      <div class="hp-verdict-why">
        <code>${esc(p.tool)}</code> on <code>${esc(p.target || "(nothing)")}</code>
        ${esc(OUTCOME[p.decision] || p.decision)}.
        <div class="hp-verdict-summary">${esc(p.summary || "")}</div>
        ${s ? `<div class="hp-dim-inline">${esc(s.text)}</div>` : ""}
        <div class="hp-dim-inline">${esc(postureText(p.posture || {}))}</div>
      </div>
    </div>
    ${invalid.length ? `<p class="hp-explain-extra" data-kind="invalid">The engine can
      never match ${esc(invalid.join(", "))} — a rule is a tool name, or a tool name
      with a pattern in brackets — so it was left out.</p>` : ""}
    ${(p.hints || []).map((h) =>
      `<p class="hp-explain-extra" data-kind="hint">${esc(h.text)}</p>`).join("")}
    ${(p.notes || []).map((n) =>
      `<p class="hp-explain-extra" data-kind="note">${esc(n)}</p>`).join("")}
    <ol class="hp-trace">${(p.steps || []).map(stepHtml).join("")}</ol>`;
}

export function explainErrorHtml(err) {
  const msg = String(err?.message || err || "").replace(/^\d+:\s*/, "");
  return `<div class="hp-degraded">The permission engine could not be asked:
    ${esc(msg)}. Nothing is shown rather than something guessed.</div>`;
}

/** A debounced asker: call it on every keystroke; `paint(result, error)` runs
 *  once typing pauses, and only for the newest question — an older answer
 *  arriving late is dropped rather than painted over a newer one. */
export function explainer(ask, paint, { delay = 250 } = {}) {
  let timer = null;
  let ticket = 0;
  return (body) => {
    clearTimeout(timer);
    const mine = ++ticket;
    timer = setTimeout(async () => {
      let result = null;
      let error = null;
      try { result = await ask(body); } catch (err) { error = err; }
      if (mine === ticket) paint(result, error);
    }, delay);
  };
}
