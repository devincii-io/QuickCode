import test from "node:test";
import assert from "node:assert/strict";
import { HISTORY_MAX, parseHistory, serializeHistory } from "../../quickcode/frontend/js/input_history.js";

test("a v1 list, a v2 record and garbage all read back as strings", () => {
  assert.deepEqual(parseHistory('["a","b"]'), ["a", "b"]);
  assert.deepEqual(parseHistory('{"v":2,"items":["a",3,"",null,"b"]}'), ["a", "b"]);
  assert.deepEqual(parseHistory("{not json"), []);
  assert.deepEqual(parseHistory(null), []);
  assert.deepEqual(parseHistory('{"v":3,"items":"nope"}'), []);
});

test("what is written reads back the same", () => {
  const items = ["fix the test", "run it again", "ship it"];
  assert.deepEqual(parseHistory(serializeHistory(items)), items);
});

test("a pasted log is recalled this session but never written to storage", () => {
  const log = "x".repeat(50_000);
  assert.deepEqual(parseHistory(serializeHistory(["short", log, "after"])), ["short", "after"]);
});

test("the stored list stays inside its budget however much is sent", () => {
  const big = Array.from({ length: HISTORY_MAX }, (_, i) => `${i} ${"y".repeat(7_000)}`);
  const text = serializeHistory(big);
  assert.ok(text.length <= 200_000, `stored ${text.length} characters`);
  const kept = parseHistory(text);
  assert.ok(kept.length > 0);
  // The newest survive; the oldest are the ones shed.
  assert.equal(kept.at(-1), big.at(-1));
  assert.ok(!kept.includes(big[0]));
});
