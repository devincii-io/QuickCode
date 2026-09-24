// @path completion.
//
// The slash menu keys off the whole input value, because a slash command *is*
// the whole message. A path is not: it is one token inside a sentence, so this
// half keys off the token under the caret instead, and it reuses the same menu
// — render, position, ↑↓, Tab/Enter, Escape — rather than growing a second
// popover with its own manners.

import { api } from "../api.js";
import { debounce } from "../util.js";

export const PATH_LIMIT = 40;

// Called when a fetch lands, so the composer can redraw its menu.
let onFetched = () => {};

export function initPaths({ refresh }) {
  onFetched = refresh;
}

// The token being typed, when it is an @ token. An `@` that is not at the
// start of a token (an email address, a decorator argument) is not one.
export function atToken(input) {
  if (!input) return null;
  const before = input.value.slice(0, input.selectionStart ?? input.value.length);
  const m = /(?:^|\s)@([^\s@]*)$/.exec(before);
  if (!m) return null;
  return { start: before.length - m[1].length - 1, end: before.length, query: m[1] };
}

// The last answer from the server, and the query it answered. Kept so that
// typing another character narrows what is already on screen instead of
// blanking the menu for the length of a round trip.
let pathCache = { query: null, entries: [] };
let pathWanted = null;

/** The rows of `cache` that answer `query`, or null when the cache cannot
 *  say anything about it yet. */
export function cachedRows(cache, query) {
  const known = cache.query;
  if (known === null) return null;
  if (!query.toLowerCase().startsWith(known.toLowerCase())) {
    // Backspaced past what the cache covers: what is there is a subset of the
    // right answer, so show it and let the fetch widen it.
    return known.toLowerCase().startsWith(query.toLowerCase()) ? cache.entries : null;
  }
  const cut = query.lastIndexOf("/") + 1;
  const head = query.slice(0, cut).toLowerCase();
  const needle = query.slice(cut).toLowerCase();
  return cache.entries.filter((p) => {
    const low = p.path.toLowerCase();
    if (!low.startsWith(head)) return false;
    const rest = low.slice(head.length);
    return rest.startsWith(needle) || rest.split("/").pop().includes(needle);
  });
}

const fetchPaths = debounce(async (query) => {
  if (pathWanted !== query) return;          // outrun by a later keystroke
  let entries = [];
  try {
    const res = await api.paths(query, PATH_LIMIT);
    entries = res.paths || [];
  } catch { /* no project, or the query escaped: an empty menu is the answer */ }
  if (pathWanted !== query) return;
  pathCache = { query, entries };
  onFetched();
}, 120);

/** Replace the @ token under the caret with `entry`. False when the caret has
 *  left the token. */
export function insertPath(input, entry) {
  const tok = atToken(input);
  if (!tok) return false;
  const text = "@" + entry.path + (entry.is_dir ? "/" : " ");
  const v = input.value;
  input.value = v.slice(0, tok.start) + text + v.slice(tok.end);
  input.selectionStart = input.selectionEnd = tok.start + text.length;
  return true;
}

/** Menu entries for `query`; each one's `insert` hands its path to `insert`. */
export function pathEntries(query, insert) {
  if (pathWanted !== query && pathCache.query !== query) {
    pathWanted = query;
    fetchPaths(query);
  }
  const rows = cachedRows(pathCache, query);
  if (!rows) return null;
  return rows.slice(0, PATH_LIMIT).map((p) => ({
    label: p.path + (p.is_dir ? "/" : ""),
    arg: p.is_dir ? "dir" : "",
    desc: "",
    insert: () => insert(p),
  }));
}
