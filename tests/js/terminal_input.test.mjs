// What the panel sends to the shell for keys, pastes and "run here".
//
// "Run here" is the one path by which text the *model* wrote reaches the
// user's unrestricted shell, and its promise is that a human keystroke is
// what runs it. Stripping newlines was not enough to keep that promise.
import test from "node:test";
import assert from "node:assert/strict";
import {
  inputChunks, keyToBytes, pasteBytes, stagedCommand,
} from "../../quickcode/frontend/js/terminal/keys.js";

globalThis.window = { getSelection: () => "" };

const key = (k, mods = {}) => ({ key: k, ctrlKey: false, altKey: false, metaKey: false, ...mods });

test("a staged command carries no control character that could run it", () => {
  // readline runs the line on ^O (operate-and-get-next) and on ^X^E after
  // the editor closes, and ^M/^J are only two of the ways to press Enter.
  const hostile = "rm -rf ~\x0f\x18\x05\x1b[201~\x03\x04\u009b\x7f\ttail\r\nnext";
  const staged = stagedCommand(hostile);
  assert.doesNotMatch(staged, /[\x00-\x1f\x7f-\x9f]/);
  assert.equal(staged, "rm -rf ~ [201~ tail next");
});

test("an ordinary command is staged as written", () => {
  assert.equal(stagedCommand('git log --oneline -5 | grep "fix"'), 'git log --oneline -5 | grep "fix"');
  assert.equal(stagedCommand("  \n "), "");
});

test("a bracketed paste is marked, and cannot close its own brackets", () => {
  const out = pasteBytes("echo one\necho two\x1b[201~\nrm -rf ~\n", true);
  assert.ok(out.startsWith("\x1b[200~") && out.endsWith("\x1b[201~"));
  const inner = out.slice(6, -6);
  assert.ok(!inner.includes("\x1b[201~"), "the paste ended itself early");
  assert.equal(inner, "echo one\recho two\rrm -rf ~\r");
});

test("an end marker rebuilt by removing another is removed too", () => {
  const inner = pasteBytes("a\x1b[20\x1b[201~1~b", true).slice(6, -6);
  assert.ok(!inner.includes("\x1b[201~"), inner);
});

test("an unbracketed paste is typed, newlines as Enter", () => {
  assert.equal(pasteBytes("a\r\nb\nc", false), "a\rb\rc");
});

test("long input is sent in pieces the server accepts, never splitting a character", () => {
  const text = "😀".repeat(40_000);
  const pieces = inputChunks(text, 1001);
  assert.equal(pieces.join(""), text);
  for (const piece of pieces) {
    assert.ok(piece.length <= 1001);
    assert.doesNotMatch(piece, /^[\udc00-\udfff]|[\ud800-\udbff]$/);
  }
  assert.deepEqual(inputChunks("", 10), []);
});

test("arrow keys follow the application cursor mode", () => {
  assert.equal(keyToBytes(key("ArrowUp")), "\x1b[A");
  assert.equal(keyToBytes(key("ArrowUp"), { appCursor: true }), "\x1bOA");
  assert.equal(keyToBytes(key("Home"), { appCursor: true }), "\x1bOH");
  assert.equal(keyToBytes(key("ArrowLeft", { altKey: true }), { appCursor: true }), "\x1bb");
  assert.equal(keyToBytes(key("Delete"), { appCursor: true }), "\x1b[3~");
});

test("Ctrl+C interrupts unless something is selected", () => {
  assert.equal(keyToBytes(key("c", { ctrlKey: true })), "\x03");
  globalThis.window = { getSelection: () => "copied" };
  try {
    assert.equal(keyToBytes(key("c", { ctrlKey: true })), null);
  } finally {
    globalThis.window = { getSelection: () => "" };
  }
});
