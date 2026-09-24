import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MODES, MODE_IDS, modeById, modeLabel } from "../../quickcode/frontend/js/modes.js";
import { MODES as HELP_MODES } from "../../quickcode/frontend/js/help/modes.js";

test("the table names exactly the engine's modes, in the engine's order", () => {
  const py = readFileSync(new URL("../../quickcode/core/permissions.py", import.meta.url), "utf8");
  const block = py.slice(py.indexOf("class Mode("), py.indexOf("class Decision("));
  const engine = [...block.matchAll(/^\s+\w+ = "([a-z-]+)"$/gm)].map((m) => m[1]);
  assert.deepEqual(MODE_IDS, engine);
});

test("every mode has a title and a one-sentence summary", () => {
  for (const m of MODES) {
    assert.match(m.title, / mode$/, m.id);
    assert.match(m.summary, /^[A-Z].*\.$/, m.id);
  }
  assert.equal(modeById("ask").title, "Ask mode");
  assert.equal(modeById("nope"), null);
});

test("don't-ask is the one mode a subagent ceiling is not offered", () => {
  assert.deepEqual(MODES.filter((m) => m.ceiling).map((m) => m.id),
    ["plan", "ask", "auto-edit", "yolo"]);
});

test("a picker label is the short name and the summary as one clause", () => {
  assert.equal(modeLabel(modeById("ask")),
    "Ask — every mutating action (writes, edits, shell) asks for permission first");
  assert.ok(modeLabel(modeById("dontask")).startsWith("Don't-ask — never prompts"));
});

test("the help table adds its decision columns to every mode", () => {
  assert.deepEqual(HELP_MODES.map((m) => m.id), MODE_IDS);
  for (const m of HELP_MODES) {
    for (const col of ["title", "summary", "what", "write", "read", "shell", "protected"]) {
      assert.equal(typeof m[col], "string", `${m.id}.${col}`);
    }
    assert.equal(typeof m.withholds, "boolean", m.id);
  }
});
