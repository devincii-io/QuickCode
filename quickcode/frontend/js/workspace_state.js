// Saved layouts contain identifiers and positions, never credentials or messages.
import { leaves } from "./split_tree.js";

export const MAX_PANES = 8;
export const MIN_RATIO = .15, MAX_RATIO = .85;
const STEP = .05;

// The server's conversation id shape; pane ids are UUIDs, which fit it too.
// Anything else in storage is corruption, and a conversation id the server
// would refuse leaves the pane stuck on an error instead of starting fresh.
export const validId = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);

export function clampRatio(ratio) {
  return Number.isFinite(ratio) ? Math.max(MIN_RATIO, Math.min(MAX_RATIO, ratio)) : .5;
}

// The window-splitter keys: arrows step, Home and End go to the bounds.
// null leaves the key alone, including modified arrows, which belong to the
// Alt+arrow pane shortcuts rather than to the divider that has focus.
export function resizeKey(ratio, { key, altKey, ctrlKey, metaKey } = {}) {
  if (altKey || ctrlKey || metaKey) return null;
  if (key === "Home") return MIN_RATIO;
  if (key === "End") return MAX_RATIO;
  const delta = ["ArrowRight", "ArrowDown"].includes(key) ? STEP : ["ArrowLeft", "ArrowUp"].includes(key) ? -STEP : 0;
  return delta ? clampRatio(clampRatio(ratio) + delta) : null;
}

export function readLayout(raw) {
  const used = new Set();
  function node(value, depth = 0) {
    if (!value || depth > MAX_PANES) return null;
    if (value.type === "pane") {
      if (!validId(value.pane) || used.has(value.pane) || used.size >= MAX_PANES) return null;
      used.add(value.pane);
      return { type: "pane", pane: value.pane };
    }
    if (value.type !== "split" || !Array.isArray(value.children)) return null;
    const a = node(value.children[0], depth + 1), b = node(value.children[1], depth + 1);
    if (!a || !b) return a || b;
    return { type: "split", dir: value.dir === "v" ? "v" : "h", ratio: clampRatio(value.ratio), children: [a, b] };
  }
  return node(raw);
}

export function restoreWorkspace(saved) {
  const project = saved?.project;
  if (typeof project?.id !== "string" || typeof project?.path !== "string") return null;
  const tree = readLayout(saved.tree);
  const stored = saved.panes && typeof saved.panes === "object" ? saved.panes : {};
  // No prototype: a pane id read from storage may be "constructor" or "__proto__".
  const panes = Object.create(null);
  for (const id of leaves(tree)) {
    const p = Object.hasOwn(stored, id) ? stored[id] : null;
    const convId = validId(p?.convId) ? p.convId : null;
    panes[id] = {
      id, convId, persisted: !!convId,
      title: typeof p?.title === "string" ? p.title.slice(0, 120) : "New agent",
    };
  }
  return {
    project: { id: project.id, path: project.path, name: typeof project.name === "string" ? project.name : "" },
    tree, panes,
    name: typeof saved.name === "string" ? saved.name.slice(0, 80) : "",
    focused: Object.hasOwn(panes, saved.focused) ? saved.focused : leaves(tree)[0] || null,
  };
}
