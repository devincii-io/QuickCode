// Adapted from QuickTerm. Copyright (c) 2026 Devin Isaac Worbis, MIT license.
// See THIRD-PARTY-NOTICES.md for the original license notice.
// The binary split tree behind both the terminal panes and the workspace
// views. Pure on purpose: no DOM, so the shape a split or a close leaves
// behind can be tested without a browser.
//
//   {"type":"split","dir":"h"|"v","ratio":r,"children":[node,node]}
//   {"type":"pane","pane":<payload>}
//
// The leaf key is called `pane` because layout.js came first; workspace
// views store their view object under the same key so pane_move.js works for
// both without a second copy of the tree surgery.

export function leaf(payload) {
  return { type: "pane", pane: payload };
}

export function leaves(node, out = []) {
  if (!node) return out;
  if (node.type === "pane") out.push(node.pane);
  else for (const child of node.children) leaves(child, out);
  return out;
}

export function findLeaf(node, payload, parent = null) {
  if (!node) return null;
  if (node.type === "pane") return node.pane === payload ? { node, parent } : null;
  for (const child of node.children) {
    const hit = findLeaf(child, payload, node);
    if (hit) return hit;
  }
  return null;
}

// null for the root, undefined when the node is not in the tree.
export function parentOf(node, target, parent = null) {
  if (node === target) return parent;
  if (!node || node.type !== "split") return undefined;
  for (const child of node.children) {
    const found = parentOf(child, target, node);
    if (found !== undefined) return found;
  }
  return undefined;
}

export function replaceChild(parent, oldNode, newNode) {
  parent.children[parent.children.indexOf(oldNode)] = newNode;
}

// Dwindle, the way a tiling window manager places a new window: it takes
// half of the focused one, cut along its longer side. A wide pane gets a
// neighbour to its right, a tall one gets a neighbour below, and repeating
// the gesture spirals inward instead of stacking slivers.
export function dwindleDir(rect) {
  const width = Number(rect?.width) || 0;
  const height = Number(rect?.height) || 0;
  return width >= height ? "h" : "v";
}

// Put `payload` beside `anchor`: the anchor's leaf becomes a split holding
// both. Returns the new root. Without an anchor in the tree the new leaf is
// docked beside the whole tree, and an empty tree becomes the leaf itself.
export function insertBeside(root, anchor, payload, dir = "h", { first = false, ratio = 0.5 } = {}) {
  const fresh = leaf(payload);
  if (!root) return fresh;
  const hit = findLeaf(root, anchor);
  const target = hit ? hit.node : root;
  const split = {
    type: "split",
    dir: dir === "v" ? "v" : "h",
    ratio,
    children: first ? [fresh, target] : [target, fresh],
  };
  if (!hit || !hit.parent) return split;
  replaceChild(hit.parent, hit.node, split);
  return root;
}

// Take `payload` out: the split that held it collapses into its other child.
// Returns the new root, null when the tree is now empty, or `root` unchanged
// when the payload was not in it.
export function removeLeaf(root, payload) {
  const hit = findLeaf(root, payload);
  if (!hit) return root;
  if (!hit.parent) return null;
  const sibling = hit.parent.children.find((child) => child !== hit.node);
  const grand = parentOf(root, hit.parent);
  if (!grand) return sibling;
  replaceChild(grand, hit.parent, sibling);
  return root;
}

// The pane that inherits the room `payload` leaves behind: the leaf of its
// sibling subtree that sat next to it. null when it has no sibling.
export function heirOf(root, payload) {
  const hit = findLeaf(root, payload);
  if (!hit?.parent) return null;
  const index = hit.parent.children.indexOf(hit.node);
  const ids = leaves(hit.parent.children[1 - index]);
  return index === 0 ? ids[0] : ids[ids.length - 1];
}

const axis = (node) => (node.dir === "v" ? "v" : "h");

// How many panes line up along `dir` inside `node`: a split along that axis
// adds its children up, a split across it is as long as its longer child.
function span(node, dir) {
  if (node?.type !== "split") return 1;
  const a = span(node.children[0], dir), b = span(node.children[1], dir);
  return axis(node) === dir ? a + b : Math.max(a, b);
}

// The ratio that gives every pane along the split's axis the same room. A
// flat 0.5 leaves three panes split right twice at a half and two quarters.
export function evenRatio(split) {
  const a = span(split.children[0], axis(split)), b = span(split.children[1], axis(split));
  return a / (a + b);
}

export function equalize(root) {
  if (root?.type !== "split") return root;
  root.ratio = evenRatio(root);
  root.children.forEach(equalize);
  return root;
}

// Where every leaf and every divider lands inside `rect`, as plain numbers.
// `gap` is the divider's thickness. Ratios are clamped the same way the
// renderer clamps them, so a stored 0.01 cannot squeeze a view to nothing.
export function layoutRects(root, rect, gap = 0, { min = 0.05, max = 0.95 } = {}) {
  const out = { leaves: new Map(), dividers: [] };
  const walk = (node, box) => {
    if (!node) return;
    if (node.type === "pane") {
      out.leaves.set(node.pane, box);
      return;
    }
    const horizontal = node.dir !== "v";
    const total = horizontal ? box.width : box.height;
    const ratio = Math.min(max, Math.max(min, typeof node.ratio === "number" ? node.ratio : 0.5));
    const firstSize = Math.max(0, Math.round((total - gap) * ratio));
    const secondSize = Math.max(0, total - gap - firstSize);
    // `box` on a divider is the whole split it sits in, which a drag needs to
    // turn a pointer position back into a ratio.
    if (horizontal) {
      walk(node.children[0], { left: box.left, top: box.top, width: firstSize, height: box.height });
      out.dividers.push({ node, box, left: box.left + firstSize, top: box.top, width: gap, height: box.height });
      walk(node.children[1], { left: box.left + firstSize + gap, top: box.top, width: secondSize, height: box.height });
    } else {
      walk(node.children[0], { left: box.left, top: box.top, width: box.width, height: firstSize });
      out.dividers.push({ node, box, left: box.left, top: box.top + firstSize, width: box.width, height: gap });
      walk(node.children[1], { left: box.left, top: box.top + firstSize + gap, width: box.width, height: secondSize });
    }
  };
  walk(root, rect);
  return out;
}
