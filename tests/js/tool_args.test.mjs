import test from "node:test";
import assert from "node:assert/strict";
import { argSummary } from "../../quickcode/frontend/js/tool_args.js";

const args = (o) => JSON.stringify(o);

test("each tool is summarised by the argument that identifies the call", () => {
  assert.equal(argSummary("bash", args({ command: "npm  test\n--watch" })), "npm test --watch");
  assert.equal(argSummary("edit", args({ file_path: "src/a.py", old_string: "x" })), "src/a.py");
  assert.equal(argSummary("read", args({ path: "README.md" })), "README.md");
  assert.equal(argSummary("grep", args({ pattern: "TODO", path: "src" })), "TODO src");
  assert.equal(argSummary("grep", args({ path: "src" })), "src");
  assert.equal(argSummary("agent", args({ definition: "explore", prompt: "p" })), "explore");
  assert.equal(argSummary("web_fetch", args({ url: "https://x", max: 3 })), "url: https://x, max: 3");
});

test("the widths are the caller's: the agents panel is narrower", () => {
  const long = "x".repeat(300);
  assert.equal(argSummary("bash", args({ command: long })).length, 140);
  assert.equal(argSummary("glob", args({ pattern: long })).length, 120);
  const narrow = { width: 100, wide: 100, each: 32 };
  assert.equal(argSummary("bash", args({ command: long }), narrow).length, 100);
  assert.equal(argSummary("other", args({ k: long }), narrow), "k: " + "x".repeat(32));
});

test("arguments that are not an object show as they came", () => {
  assert.equal(argSummary("bash", '{"command": "ls'), '{"command": "ls');
  assert.equal(argSummary("bash", "null"), "null");
  assert.equal(argSummary("read", ""), "");
});
