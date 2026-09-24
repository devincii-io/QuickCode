import test from "node:test";
import assert from "node:assert/strict";
import {
  EVENTS, checkDraft, checkMatcher, checkTimeout, defaultTool, draftBody, groupByEvent,
  hookHref, matcherText, parseToolInput,
} from "../../quickcode/frontend/js/config/hooks_model.js";

test("the events are the loader's five, in its order", () => {
  assert.deepEqual(EVENTS.map((e) => e.name),
    ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart"]);
  assert.deepEqual(EVENTS.filter((e) => e.tool).map((e) => e.name),
    ["PreToolUse", "PostToolUse"]);
});

test("a matcher is tool names and globs, the way the server reads it", () => {
  for (const ok of ["", "*", "bash", "write|edit", "mcp__*", " Bash | task_? ",
    "mcp__company-kb__kb.search", "[be]ash", "é_tool"]) {
    assert.equal(checkMatcher("PreToolUse", ok), "", ok);
  }
  const bad = {
    "mcp__docs__.*": /regular expression.*mcp__docs__\*/,
    "^bash$": /regular expression/,
    "bash|": /empty alternative/,
    "bash||edit": /empty alternative/,
    "bash tool": /not a tool name or a glob/,
    "[ab": /unbalanced/,
    "ab]": /unbalanced/,
  };
  for (const [matcher, words] of Object.entries(bad)) {
    assert.match(checkMatcher("PreToolUse", matcher), words, matcher);
  }
});

test("a matcher on an event that is not about a tool is refused, not ignored", () => {
  assert.match(checkMatcher("Stop", "bash"), /not about a tool/);
  assert.equal(checkMatcher("Stop", ""), "");
  assert.equal(checkMatcher("SessionStart", "*"), "");
});

test("a timeout is optional, and bounded when given", () => {
  assert.deepEqual(checkTimeout(""), { value: null, error: "" });
  assert.deepEqual(checkTimeout("12"), { value: 12, error: "" });
  assert.deepEqual(checkTimeout(600), { value: 600, error: "" });
  for (const bad of ["0", "-3", "601", "ten", "Infinity"]) {
    assert.ok(checkTimeout(bad).error, bad);
  }
});

test("a draft collects every problem by field, and sends only what it should", () => {
  assert.deepEqual(checkDraft({ event: "PreToolUse", matcher: "bash", command: "g.sh",
    timeout: "" }), {});
  const errors = checkDraft({ event: "Stop", matcher: "bash", command: "  ", timeout: "0" });
  assert.deepEqual(Object.keys(errors).sort(), ["command", "matcher", "timeout"]);
  assert.ok(checkDraft({ event: "Nope", command: "x" }).event);
  assert.match(checkDraft({ event: "Stop", command: "a\0b" }).command, /NUL/);

  assert.deepEqual(
    draftBody({ event: "PreToolUse", matcher: " bash ", command: " g.sh ", timeout: "9" }),
    { event: "PreToolUse", matcher: "bash", command: "g.sh", timeout: 9 });
  // A matcher left in the box after switching to Stop is not sent.
  assert.deepEqual(draftBody({ event: "Stop", matcher: "bash", command: "n.sh", timeout: "" }),
    { event: "Stop", matcher: "", command: "n.sh" });
});

test("every event gets a group, in order, empty ones included", () => {
  const hooks = [
    { id: "a", event: "Stop" }, { id: "b", event: "PreToolUse" }, { id: "c", event: "Stop" },
  ];
  const groups = groupByEvent(hooks);
  assert.deepEqual(groups.map((g) => g.event.name), EVENTS.map((e) => e.name));
  assert.deepEqual(groups.map((g) => g.hooks.map((x) => x.id)),
    [["b"], [], [], ["a", "c"], []]);
});

test("the test panel starts from a tool the matcher names outright", () => {
  assert.equal(defaultTool("write|edit"), "write");
  assert.equal(defaultTool("Bash"), "bash");
  assert.equal(defaultTool("mcp__*|read"), "read");
  assert.equal(defaultTool("mcp__*"), "bash");
  assert.equal(defaultTool(""), "bash");
  assert.equal(matcherText({ event: "PreToolUse", matcher: "" }), "every tool");
  assert.equal(matcherText({ event: "PreToolUse", matcher: "bash" }), "bash");
  assert.equal(matcherText({ event: "Stop", matcher: "" }), "");
});

test("tool arguments are a JSON object or nothing", () => {
  assert.deepEqual(parseToolInput(""), { value: null, error: "" });
  assert.deepEqual(parseToolInput('{"command": "ls"}'), { value: { command: "ls" }, error: "" });
  assert.match(parseToolInput("[1]").error, /JSON object/);
  assert.match(parseToolInput("null").error, /JSON object/);
  assert.match(parseToolInput("{nope").error, /Not JSON/);
});

test("a hook's page names its file, since one command may sit in two", () => {
  assert.equal(hookHref({ id: "hook.cmd.project.stop.0123456789", file: "settings.local.json" }),
    "#/config/hooks/hook.cmd.project.stop.0123456789?file=settings.local.json");
});
