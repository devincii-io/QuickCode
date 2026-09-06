// Saved layouts contain identifiers and positions, never credentials or messages.
import { leaves } from "./split_tree.js";

export const MAX_PANES = 8;
export function readLayout(raw) {
  const used = new Set();
  function node(value, depth = 0) {
    if (!value || depth > MAX_PANES) return null;
    if (value.type === "pane") {
      if (typeof value.pane !== "string" || used.has(value.pane) || used.size >= MAX_PANES) return null;
      used.add(value.pane);
      return { type: "pane", pane: value.pane };
    }
    if (value.type !== "split" || !Array.isArray(value.children)) return null;
    const a = node(value.children[0], depth + 1), b = node(value.children[1], depth + 1);
    if (!a || !b) return a || b;
    return { type: "split", dir: value.dir === "v" ? "v" : "h",
      ratio: Number.isFinite(value.ratio) ? Math.max(.15, Math.min(.85, value.ratio)) : .5,
      children: [a, b] };
  }
  return node(raw);
}

export function restoreWorkspace(saved) {
  if (!saved || typeof saved.project?.id !== "string" || typeof saved.project?.path !== "string") return null;
  let tree = readLayout(saved.tree);
  const panes = {};
  for (const id of leaves(tree)) {
    const p = saved.panes?.[id];
    panes[id] = {
      id, convId: typeof p?.convId === "string" ? p.convId : null,
      persisted: typeof p?.convId === "string",
      title: typeof p?.title === "string" ? p.title.slice(0, 120) : "New agent",
    };
  }
  return { project: saved.project, tree, panes,
    name: typeof saved.name === "string" ? saved.name.slice(0, 80) : "",
    focused: panes[saved.focused] ? saved.focused : leaves(tree)[0] || null };
}
