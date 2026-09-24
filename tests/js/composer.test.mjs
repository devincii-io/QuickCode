import test from "node:test";
import assert from "node:assert/strict";

const stored = new Map();
globalThis.localStorage = {
  getItem: (k) => (stored.has(k) ? stored.get(k) : null),
  setItem: (k, v) => stored.set(k, String(v)),
};

const { setProject } = await import("../../quickcode/frontend/js/api.js");
const { historyBack, historyForward, remember, resetWalk } =
  await import("../../quickcode/frontend/js/composer/history.js");
const { atToken, cachedRows, insertPath } =
  await import("../../quickcode/frontend/js/composer/paths.js");

test("↑ walks only what starts with the typed prefix, ↓ returns to the draft", () => {
  setProject("alpha");
  for (const t of ["git status", "run the tests", "git push", "git status"]) remember(t);
  assert.equal(historyBack("git"), "git status");   // deduped, newest first
  assert.equal(historyBack("ignored mid-walk"), "git push");
  assert.equal(historyBack(""), "git push");           // the oldest match holds
  assert.equal(historyForward(), "git status");
  assert.equal(historyForward(), "git");               // back to what was typed
  assert.equal(historyForward(), null);                // no walk under way
  assert.equal(historyBack("nothing like it"), null);
});

test("each project keeps its own list, in its own storage key", () => {
  resetWalk();
  setProject("beta");
  assert.equal(historyBack(""), null);
  remember("only in beta");
  assert.equal(historyBack(""), "only in beta");
  resetWalk();
  setProject("alpha");
  assert.equal(historyBack(""), "git status");
  assert.ok(stored.has("qc-history:alpha") && stored.has("qc-history:beta"));
});

const field = (value, caret = value.length) => ({ value, selectionStart: caret, selectionEnd: caret });

test("an @ starts a path token only at the start of a word", () => {
  assert.deepEqual(atToken(field("see @src/ma")), { start: 4, end: 11, query: "src/ma" });
  assert.equal(atToken(field("mail me@example.com")), null);
  assert.equal(atToken(field("@a b")), null);
});

test("choosing a path rewrites the token; a directory leaves the caret inside it", () => {
  const input = field("open @sr and more", 8);
  assert.equal(insertPath(input, { path: "src", is_dir: true }), true);
  assert.equal(input.value, "open @src/ and more");
  assert.equal(input.selectionStart, 10);
  assert.equal(insertPath(field("no token"), { path: "x" }), false);
});

test("the cached answer narrows as the query grows and is dropped when it cannot", () => {
  const cache = { query: "src/", entries: [
    { path: "src/main.py" }, { path: "src/util/strings.py" }, { path: "src/README.md" },
  ] };
  assert.deepEqual(cachedRows(cache, "src/ma").map((p) => p.path), ["src/main.py"]);
  assert.deepEqual(cachedRows(cache, "src/str").map((p) => p.path), ["src/util/strings.py"]);
  assert.equal(cachedRows(cache, "src").length, 3);   // backspaced: a subset, still shown
  assert.equal(cachedRows(cache, "lib"), null);
  assert.equal(cachedRows({ query: null, entries: [] }, "x"), null);
});
