import test from "node:test";
import assert from "node:assert/strict";
import { replacementLines, unifiedLines } from "../../quickcode/frontend/js/diff.js";
import { offerSummary } from "../../quickcode/frontend/js/permission_offer.js";

// What tools/fs/diffpreview.py sends for an edit.
const DIFF = [
  "--- /p/a.md",
  "+++ /p/a.md",
  "@@ -1,3 +1,3 @@",
  " # Title",
  "--- a rule, deleted",
  "+++ a rule, added",
  "-old",
  "+new",
  "… 12 more diff lines not shown",
].join("\n");

test("file headers are only headers above the first hunk", () => {
  assert.deepEqual(unifiedLines(DIFF).map((l) => l.kind), [
    "file", "file", "hunk", "ctx", "del", "add", "del", "add", "note",
  ]);
});

test("a line keeps its text exactly, markup included", () => {
  const [line] = unifiedLines("+<img src=x onerror=alert(1)>");
  assert.deepEqual(line, { kind: "add", text: "+<img src=x onerror=alert(1)>" });
});

test("an edit's two strings become removed then added lines", () => {
  assert.deepEqual(replacementLines("a\nb", "c"), [
    { kind: "del", text: "- a" }, { kind: "del", text: "- b" }, { kind: "add", text: "+ c" },
  ]);
});

test("the offer lists the exact rules and names what still asks", () => {
  const offer = offerSummary({
    rules: ["bash(npm test)"],
    kept: [{ part: "cat .env", reason: "protected_path" }],
  });
  assert.deepEqual(offer.rules, ["bash(npm test)"]);
  assert.equal(offer.canSave, true);
  assert.deepEqual(offer.kept, [
    { part: "cat .env", why: "touches a protected path, which asks every time" },
  ]);
});

test("nothing to save disables the offer and says why", () => {
  const breaker = offerSummary({ rules: [], kept: [{ part: "git push -f", reason: "circuit_breaker" }] });
  assert.equal(breaker.canSave, false);
  assert.match(breaker.empty, /nothing to save/);
  const hooked = offerSummary({ rules: [], kept: [], hook_reason: "double-check writes" });
  assert.match(hooked.empty, /the hook asks each time/);
});

test("a request logged before the rule list still shows its one rule", () => {
  const offer = offerSummary({ rule_suggestion: "bash(npm *)" });
  assert.deepEqual(offer.rules, ["bash(npm *)"]);
  assert.equal(offer.canSave, true);
});

test("an unknown reason is shown by its id rather than dropped", () => {
  const offer = offerSummary({ rules: [], kept: [{ part: "x", reason: "something_new" }] });
  assert.deepEqual(offer.kept, [{ part: "x", why: "something_new" }]);
});
