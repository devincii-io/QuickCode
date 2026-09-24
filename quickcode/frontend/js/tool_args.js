// A tool call's arguments as one line, for the lists that show calls: the
// transcript's tool cards and the agents panel.

import { oneLine } from "./util.js";

/** `width` caps a path or a pattern; `wide` caps a command and the generic
 *  `key: value` list; `each` caps one value inside that list. Arguments that
 *  are not a JSON object (a cut-off stream, a bare `null`) show as they came. */
export function argSummary(name, argsRaw, { width = 120, wide = 140, each = 40 } = {}) {
  let a;
  try { a = JSON.parse(argsRaw || "{}"); } catch { return oneLine(argsRaw, width); }
  if (!a || typeof a !== "object") return oneLine(argsRaw, width);
  if (name === "bash") return oneLine(a.command, wide);
  if (name === "read" || name === "write" || name === "edit") {
    return oneLine(a.file_path || a.path, width);
  }
  if (name === "grep") return oneLine(`${a.pattern ?? ""}  ${a.path || ""}`, width);
  if (name === "glob") return oneLine(a.pattern, width);
  if (name === "agent") return oneLine(a.definition || a.prompt, width);
  return oneLine(
    Object.entries(a).map(([k, v]) => `${k}: ${oneLine(String(v), each)}`).join(", "), wide);
}
