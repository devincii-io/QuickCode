import test from "node:test";
import assert from "node:assert/strict";
import {
  explainErrorHtml, explainHtml, explainer,
} from "../../quickcode/frontend/js/help/explain.js";

// The shape POST /api/permissions/explain answers with (core/permission_explain.py).
const payload = {
  tool: "bash",
  target: "npm test && rm -rf build",
  decision: "ask",
  summary: "'rm -rf build': No rule matched, so ask mode prompts for the command.",
  decided_by: { step: "mode_default", decision: "ask", command: "rm -rf build" },
  steps: [
    { step: "shell", decision: null, why: "Split into 2 subcommands." },
    {
      step: "subcommand", decision: "allow", command: "npm test", why: "Matched the allow rule.",
      steps: [{ step: "allow_rule", decision: "allow", rule: "bash(npm *)",
                why: "Matched the allow rule bash(npm *)." }],
    },
    {
      step: "subcommand", decision: "ask", command: "rm -rf build", why: "No rule matched.",
      steps: [{ step: "mode_default", decision: "ask", why: "No rule matched." }],
    },
    { step: "most_restrictive", decision: "ask", why: "Most restrictive answer: ask." },
  ],
  posture: { mode: "ask", mode_source: "request", profile: "", trusted: false,
             project_rules: true },
  suggestion: { rule: "bash(npm *)", text: "Always allow would write bash(npm *)." },
  hints: [{ kind: "untrusted_allow", text: "This project is not trusted." }],
  notes: ["1 PreToolUse hook matches this tool."],
  invalid_rules: [],
};

test("the verdict, the reason and every step are drawn from the payload", () => {
  const html = explainHtml(payload);
  assert.match(html, /data-outcome="ask"/);
  assert.match(html, /No rule matched, so ask mode prompts/);
  assert.match(html, /Always allow would write bash\(npm \*\)/);
  assert.match(html, /This project is not trusted/);
  assert.match(html, /PreToolUse hook/);
  assert.match(html, /project not trusted/);
  // Each subcommand carries its own checks, nested under it.
  assert.equal((html.match(/hp-trace-sub/g) || []).length, 2);
  assert.match(html, /Matched the allow rule bash\(npm \*\)/);
  // A step that looked and did not decide is marked as such.
  assert.match(html, /data-hit="skip"/);
});

test("everything the payload says is escaped, including what the user typed", () => {
  const html = explainHtml({
    ...payload,
    target: "<img src=x onerror=alert(1)>",
    summary: "<b>not bold</b>",
    steps: [{ step: "<script>", decision: "ask", why: "\"quoted\" & <tagged>" }],
    invalid_rules: ["deny: bash(<x>"],
  });
  assert.doesNotMatch(html, /<img|<script>|<b>not/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&quot;quoted&quot; &amp; &lt;tagged&gt;/);
  assert.match(html, /never\s+match deny: bash\(&lt;x&gt;/);
});

test("an error says the engine could not be asked, without the status prefix", () => {
  const html = explainErrorHtml(new Error("404: no tool 'nope' in this project"));
  assert.match(html, /could not be asked/);
  assert.match(html, /no tool &#39;nope&#39;/);
  assert.doesNotMatch(html, /404:/);
});

test("only the newest question is painted, however the answers arrive", async () => {
  const pending = [];
  const painted = [];
  const ask = explainer(
    (body) => new Promise((resolve) => pending.push(() => resolve(body.n))),
    (result, error) => painted.push(error ? "error" : result),
    { delay: 0 },
  );
  ask({ n: 1 });
  await new Promise((r) => setTimeout(r, 5));
  ask({ n: 2 });
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(pending.length, 2);
  pending[1]();          // the newer answer first...
  pending[0]();          // ...then the stale one
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(painted, [2]);
});

test("keystrokes inside the delay collapse into one question", async () => {
  const asked = [];
  const ask = explainer(async (body) => { asked.push(body.n); return body.n; }, () => {},
    { delay: 10 });
  ask({ n: 1 });
  ask({ n: 2 });
  ask({ n: 3 });
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(asked, [3]);
});
