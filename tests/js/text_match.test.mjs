import test from "node:test";
import assert from "node:assert/strict";
import { highlightHtml, matchesAll, queryTerms } from "../../quickcode/frontend/js/text_match.js";

test("terms split on whitespace, quoted runs stay whole, duplicates drop", () => {
  assert.deepEqual(queryTerms('  Login  "Redirect  LOOP" login '), ["login", "redirect loop"]);
  assert.deepEqual(queryTerms('stray " quote'), ["stray", "quote"]);
  assert.deepEqual(queryTerms(""), []);
});

test("every term must occur, in any case", () => {
  const terms = queryTerms("page LOGIN");
  assert.equal(matchesAll("The login page", terms), true);
  assert.equal(matchesAll("The login form", terms), false);
  assert.equal(matchesAll("anything", []), true);
});

test("highlighting escapes the text and marks each occurrence once", () => {
  assert.equal(highlightHtml("a <b> and B", ["b"]),
    "a &lt;<mark>b</mark>&gt; and <mark>B</mark>");
  // Overlapping terms merge instead of nesting marks.
  assert.equal(highlightHtml("abcdef", ["abc", "bcd"]), "<mark>abc</mark><mark>d</mark>ef");
  assert.equal(highlightHtml("<x>", []), "&lt;x&gt;");
});
