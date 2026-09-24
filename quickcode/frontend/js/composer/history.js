// Sent-message recall: ↑ and ↓ in the composer.
//
// One list per project, like the side panel's layout: the messages you sent to
// one repo are noise in the next. Nothing here touches the DOM — the composer
// hands in what is typed and puts back whatever this returns.

import { currentProject } from "../api.js";
import { HISTORY_MAX, parseHistory, serializeHistory } from "../input_history.js";

const HISTORY_KEY = "qc-history";

let history = [];
let historyPid = null;
// The recall walk. `walk` is null while the draft is being edited; once ↑ is
// pressed it holds the entries that match what was already typed, and it is
// that filtered list — not the whole history — that ↑/↓ move through.
let walk = null;
let walkIdx = 0;
let draft = "";

function historyKey() { return `${HISTORY_KEY}:${currentProject() || "default"}`; }

function loadHistory() {
  try { return parseHistory(localStorage.getItem(historyKey())); } catch { return []; }
}

function saveHistory(h) {
  try {
    localStorage.setItem(historyKey(), serializeHistory(h));
  } catch { /* quota / private mode */ }
}

// The composer is wired once, before any project is open, so the list is
// (re)read the first time each project needs it.
function syncHistory() {
  const pid = currentProject() || "default";
  if (pid === historyPid) return;
  historyPid = pid;
  history = loadHistory();
  resetWalk();
}

export function resetWalk() {
  walk = null;
  walkIdx = 0;
  draft = "";
}

export function remember(text) {
  syncHistory();
  // Full dedupe, not just the adjacent one: the same command sent five times
  // over an afternoon should cost one line of recall, not five presses of ↑.
  const next = history.filter((h) => h !== text);
  next.push(text);
  history = next.length > HISTORY_MAX ? next.slice(-HISTORY_MAX) : next;
  saveHistory(history);
  resetWalk();
}

// What ↑ walks: the entries that start with what is already typed. An empty
// composer walks everything, which is the old behavior unchanged.
function matching(text) {
  const q = text.trim();
  if (!q) return history.slice();
  const low = q.toLowerCase();
  return history.filter((h) => h !== q && h.toLowerCase().startsWith(low));
}

/** ↑ with `typed` in the composer: the entry to show instead, or null when
 *  nothing matches. */
export function historyBack(typed) {
  syncHistory();
  if (walk === null) {
    // The prefix is fixed when the walk starts and holds for the whole walk;
    // re-filtering on every press would use the recalled line as the filter.
    draft = typed;
    walk = matching(draft);
    walkIdx = walk.length;
    if (!walk.length) { walk = null; return null; }
  }
  if (walkIdx > 0) walkIdx--;
  return walk[walkIdx];
}

/** ↓: the next entry, or the draft the walk started from once it runs out.
 *  Null when no walk is under way. */
export function historyForward() {
  if (walk === null) return null;
  if (walkIdx < walk.length - 1) {
    walkIdx++;
    return walk[walkIdx];
  }
  const back = draft;
  resetWalk();
  return back;
}
