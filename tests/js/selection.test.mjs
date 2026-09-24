import test from "node:test";
import assert from "node:assert/strict";

// toast.js builds its host on first use; a node that remembers what was said
// is all reportBulk needs.
const said = [];
const node = () => ({
  isConnected: true, className: "", dataset: {}, classList: { add() {}, remove() {} },
  setAttribute() {}, addEventListener() {}, appendChild(n) { return n; }, remove() {},
  querySelector: () => node(), querySelectorAll: () => [], children: [],
  set innerHTML(html) { said.push(html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()); },
});
globalThis.document = { body: node(), createElement: node };
globalThis.setTimeout = () => 0;

const { makeSelection, reportBulk } = await import("../../quickcode/frontend/js/selection.js");

test("shift extends from the anchor and gives the range the clicked row's new state", () => {
  const order = ["a", "b", "c", "d", "e"];
  const sel = makeSelection();
  sel.toggle("b", order);
  sel.toggle("d", order, true);
  assert.deepEqual(sel.inOrder(order), ["b", "c", "d"]);
  sel.toggle("c", order, true);     // c is on, so the range c..d turns off
  assert.deepEqual(sel.inOrder(order), ["b"]);
  sel.toggle("e", order, true);     // anchor c, e is off: c..e on
  assert.deepEqual(sel.inOrder(order), ["b", "c", "d", "e"]);
});

test("a row that left the screen leaves the count; clear says whether it did anything", () => {
  const sel = makeSelection();
  sel.setAll(["a", "b", "c"], true);
  sel.keepOnly(["a", "c"]);
  assert.equal(sel.size, 2);
  assert.equal(sel.has("b"), false);
  assert.equal(sel.clear(), true);
  assert.equal(sel.clear(), false);
});

test("a partial bulk result never reads as a plain success", () => {
  said.length = 0;
  reportBulk(3, [], { one: "session", many: "sessions" });
  reportBulk(1, [{ reason: "live" }, { reason: "live" }, { reason: "odd" }], { one: "session", many: "sessions" });
  reportBulk(0, [{ reason: "missing" }], { one: "project", many: "projects" });
  const text = said.join("\n");
  assert.match(text, /Deleted 3 sessions\./);
  assert.match(text, /Deleted 1 session; 3 left alone \(2 still open, 1 skipped\)\./);
  assert.match(text, /Nothing was deleted — 1 already gone\./);
});
