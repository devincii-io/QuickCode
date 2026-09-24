// The shared display formatters in js/util.js.
import test from "node:test";
import assert from "node:assert/strict";
import { fmtBytes, fmtChars, fmtMs, plural } from "../../quickcode/frontend/js/util.js";

test("a duration measured with performance.now() reads as whole milliseconds", () => {
  // The status bar's time to first token is a performance.now() difference.
  assert.equal(fmtMs(734.2999999523163), "734 ms");
  assert.equal(fmtMs(0), "0 ms");
  assert.equal(fmtMs(null), "");
  assert.equal(fmtMs(undefined), "");
});

test("a duration picks its unit from the rounded value and grows into minutes and hours", () => {
  assert.equal(fmtMs(999.7), "1.0 s");
  assert.equal(fmtMs(3_240), "3.2 s");
  assert.equal(fmtMs(59_970), "1m 0s");
  assert.equal(fmtMs(754_300), "12m 34s");
  assert.equal(fmtMs(3 * 3_600_000 + 7 * 60_000), "3h 7m");
  assert.equal(fmtMs(-3), "0 ms", "a live start against a replayed end is not negative time");
});

test("sizes in bytes roll over on the rounded value", () => {
  assert.deepEqual([0, 1023, 1536, 18_233, 3 * 1024 * 1024, 134_217_728, 1024 * 1024 - 1].map(fmtBytes),
    ["0 B", "1023 B", "1.5 KB", "17.8 KB", "3.0 MB", "128.0 MB", "1.0 MB"]);
  assert.equal(fmtBytes(undefined), "0 B");
  assert.equal(fmtBytes(-5), "0 B");
});

test("text sizes count characters", () => {
  assert.deepEqual([12, 999, 1_000, 45_600, 2_345_678].map(fmtChars),
    ["12 chars", "999 chars", "1.0k chars", "45.6k chars", "2.35M chars"]);
});

test("a count and its noun agree", () => {
  assert.equal(plural(1, "file"), "1 file");
  assert.equal(plural(0, "file"), "0 files");
  assert.equal(plural(2, "match", "matches"), "2 matches");
  assert.equal(plural(12_345, "line"), `${(12_345).toLocaleString()} lines`);
});
