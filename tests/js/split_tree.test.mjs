import test from "node:test";
import assert from "node:assert/strict";
import { equalize, evenRatio, heirOf, insertBeside, layoutRects, leaves, removeLeaf } from "../../quickcode/frontend/js/split_tree.js";

const widths = (tree, width = 1206) => {
  const rects = layoutRects(tree, { left: 0, top: 0, width, height: 600 }, 6).leaves;
  return Object.fromEntries([...rects].map(([id, r]) => [id, r.width]));
};

test("equalize gives three panes split right twice a third each", () => {
  let tree = insertBeside(null, null, "a");
  tree = insertBeside(tree, "a", "b", "h");
  tree = insertBeside(tree, "b", "c", "h");
  equalize(tree);
  const w = widths(tree);
  // Within one divider's width: the nested split pays for its own gap.
  assert.ok(Math.abs(w.a - w.b) <= 6 && w.b === w.c, JSON.stringify(w));
  assert.deepEqual([tree.ratio, tree.children[1].ratio], [1 / 3, .5]);
});

test("equalize sizes a split by the panes lined up along its axis", () => {
  // a | (b / (c | d)): the widest row under the right side holds two panes.
  let tree = insertBeside(null, null, "a");
  tree = insertBeside(tree, "a", "b", "h");
  tree = insertBeside(tree, "b", "c", "v");
  tree = insertBeside(tree, "c", "d", "h");
  equalize(tree);
  assert.equal(tree.ratio, 1 / 3);
  assert.equal(tree.children[1].ratio, .5);
  assert.equal(tree.children[1].children[1].ratio, .5);
  // A split across the axis does not count its panes twice.
  let dwindle = insertBeside(null, null, "a");
  dwindle = insertBeside(dwindle, "a", "b", "h");
  dwindle = insertBeside(dwindle, "b", "c", "v");
  assert.equal(evenRatio(dwindle), .5);
  assert.equal(equalize(insertBeside(null, null, "solo")).type, "pane");
  assert.equal(equalize(null), null);
});

test("the pane that took the closed pane's room is its heir", () => {
  let tree = insertBeside(null, null, "a");
  tree = insertBeside(tree, "a", "b", "h");
  tree = insertBeside(tree, "b", "c", "v");
  tree = insertBeside(tree, "c", "d", "h");
  // a | (b / (c | d))
  assert.equal(heirOf(tree, "a"), "b");
  assert.equal(heirOf(tree, "b"), "c");
  assert.equal(heirOf(tree, "d"), "c");
  assert.equal(heirOf(tree, "c"), "d");
  tree = insertBeside(tree, "a", "e", "v");
  // (a / e) | ...: closing e hands its room to a, not to the first pane on screen.
  assert.equal(heirOf(tree, "e"), "a");
  const heir = heirOf(tree, "c");
  tree = removeLeaf(tree, "c");
  assert.ok(leaves(tree).includes(heir));
  assert.equal(heirOf(insertBeside(null, null, "solo"), "solo"), null);
  assert.equal(heirOf(tree, "missing"), null);
});
