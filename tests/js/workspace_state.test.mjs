import test from "node:test";
import assert from "node:assert/strict";
import { insertBeside, removeLeaf, leaves, layoutRects, dwindleDir } from "../../quickcode/frontend/js/split_tree.js";
import { readLayout, restoreWorkspace, MAX_PANES } from "../../quickcode/frontend/js/workspace_state.js";

test("nested split, move and close preserve each independent pane exactly once", () => {
  let tree = insertBeside(null, null, "agent-a");
  tree = insertBeside(tree, "agent-a", "agent-b", "h");
  tree = insertBeside(tree, "agent-b", "agent-c", "v");
  tree = removeLeaf(tree, "agent-c");
  tree = insertBeside(tree, "agent-a", "agent-c", "h", { first: true });
  assert.deepEqual(leaves(tree), ["agent-c", "agent-a", "agent-b"]);
  tree = removeLeaf(tree, "agent-a");
  assert.deepEqual(leaves(tree), ["agent-c", "agent-b"]);
  tree = removeLeaf(tree, "agent-c");
  assert.deepEqual(tree, { type: "pane", pane: "agent-b" });
  assert.equal(removeLeaf(tree, "agent-b"), null);
});

test("layout tiles the available rectangle without moving or losing a pane", () => {
  let tree = insertBeside(null, null, "a");
  tree = insertBeside(tree, "a", "b", "h", { ratio: .4 });
  tree = insertBeside(tree, "b", "c", "v");
  const result = layoutRects(tree, { left: 0, top: 0, width: 1000, height: 800 }, 6);
  const a = result.leaves.get("a"), b = result.leaves.get("b"), c = result.leaves.get("c");
  assert.equal(a.width + b.width + 6, 1000);
  assert.equal(b.height + c.height + 6, 800);
  assert.equal(c.left, b.left);
  assert.equal(b.left, a.width + 6);
  assert.equal(dwindleDir(a), "v");
  assert.equal(dwindleDir(b), "h");
});

test("saved layouts reject malformed branches, duplicates and invalid ratios", () => {
  const tree = readLayout({ type: "split", dir: "wrong", ratio: Infinity,
    children: [{ type: "pane", pane: "a" }, { type: "split", dir: "v", ratio: -20,
      children: [{ type: "pane", pane: "a" }, { type: "pane", pane: "b" }] }] });
  assert.deepEqual(leaves(tree), ["a", "b"]);
  assert.equal(tree.ratio, .5);
  assert.equal(tree.dir, "h");
  assert.equal(readLayout({ type: "split", children: "broken" }), null);
  let many = null;
  for (let i = 0; i < 20; i++) many = insertBeside(many, String(i - 1), String(i));
  assert.ok(leaves(readLayout(many)).length <= MAX_PANES);
});

test("restore binds conversations to their project and repairs stale focus", () => {
  const ws = restoreWorkspace({ project: { id: "project-a", path: "C:/project-a" },
    focused: "missing", tree: { type: "pane", pane: "pane-a" },
    panes: { "pane-a": { convId: "conversation-a", title: "Review" }, orphan: { convId: "other" } } });
  assert.equal(ws.focused, "pane-a");
  assert.equal(ws.panes["pane-a"].convId, "conversation-a");
  assert.equal(ws.panes.orphan, undefined);
  assert.equal(restoreWorkspace({ project: { id: "missing-path" } }), null);
});

test("stored ids that name Object.prototype members cannot pose as panes", () => {
  const ws = restoreWorkspace({ project: { id: "p", path: "/p" }, focused: "toString",
    tree: { type: "split", dir: "h", ratio: .5,
      children: [{ type: "pane", pane: "__proto__" }, { type: "pane", pane: "constructor" }] },
    panes: JSON.parse('{"__proto__": {"convId": "kept-1", "title": "Kept"}}') });
  assert.deepEqual(Object.keys(ws.panes), ["__proto__", "constructor"]);
  assert.equal(ws.panes.__proto__.convId, "kept-1");
  assert.equal(ws.panes.constructor.convId, null);
  assert.equal(ws.focused, "__proto__");
  assert.equal(JSON.parse(JSON.stringify(ws.panes)).__proto__.title, "Kept");
});

test("a conversation id the server would refuse restores as a fresh conversation", () => {
  const ws = restoreWorkspace({ project: { id: "p", path: "/p", name: { not: "text" }, extra: "dropped" },
    tree: { type: "split", children: [{ type: "pane", pane: "a" }, { type: "pane", pane: "b" }] },
    panes: { a: { convId: "bad id/.." }, b: { convId: "20260906-abc_DEF" } } });
  assert.deepEqual({ ...ws.panes.a }, { id: "a", convId: null, persisted: false, title: "New agent" });
  assert.equal(ws.panes.b.convId, "20260906-abc_DEF");
  assert.equal(ws.panes.b.persisted, true);
  assert.deepEqual(ws.project, { id: "p", path: "/p", name: "" });
  assert.equal(readLayout({ type: "split", children: [
    { type: "pane", pane: "../x" }, { type: "pane", pane: "x".repeat(65) }] }), null);
  assert.equal(readLayout({ type: "pane", pane: 7 }), null);
  assert.equal(restoreWorkspace({ project: { id: "p", path: "/p" }, panes: null, tree: null }).focused, null);
});
