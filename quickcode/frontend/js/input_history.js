// The composer's input history as it is written to localStorage. Pure — the
// composer owns the storage calls — so the rules below can be tested without a
// browser.
//
// v1 was a bare string[]. v2 is {v, items} — same strings, but a shape that can
// grow. The bump matters because v1's reader hard-filtered to strings and would
// have silently eaten anything else; v2 reads v1 and rewrites it, and a v1
// reader handed v2 sees "no history" rather than a crash.

export const HISTORY_MAX = 100;
const HISTORY_VERSION = 2;

// Recall is for what you typed, not for the log you pasted: an entry longer than
// this is still recalled for the rest of the session, but it is not written down.
const ENTRY_MAX = 8_000;

// localStorage is one small quota for the whole origin — the workspace layout and
// the panels' state live there too — and every project keeps its own list. A
// hundred pasted logs used to fill it, after which this save and every other
// write on the page failed quietly. Past this many characters the oldest go.
const BUDGET = 200_000;

export function parseHistory(text) {
  try {
    const raw = JSON.parse(text || "null");
    // A v1 list is still perfectly good data: it is read as-is and rewritten in
    // the new shape by the next send, rather than thrown away.
    const items = Array.isArray(raw) ? raw : (raw?.v >= 2 ? raw.items : []);
    return (Array.isArray(items) ? items : []).filter((x) => typeof x === "string" && x);
  } catch { return []; }
}

export function serializeHistory(items) {
  let kept = items.filter((x) => x.length <= ENTRY_MAX).slice(-HISTORY_MAX);
  let text = JSON.stringify({ v: HISTORY_VERSION, items: kept });
  while (text.length > BUDGET && kept.length) {
    kept = kept.slice(Math.ceil(kept.length / 4));
    text = JSON.stringify({ v: HISTORY_VERSION, items: kept });
  }
  return text;
}
