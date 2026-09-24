// The terminal emulator's parser, fed the input it will actually meet: a pty
// stream cut at arbitrary points, and — through renderAnsiBlock — tool results
// from MCP servers and plugins, which nobody vets. Every case here either hung
// the tab, threw, drew garbage, or leaked memory before.
import test from "node:test";
import assert from "node:assert/strict";
import {
  Emulator, MAX_SCROLLBACK, lineHtml, renderAnsiBlock,
} from "../../quickcode/frontend/js/terminal/emulator.js";

const text = (line) => line.chars.join("");
const screenText = (emu) => emu.screen.map(text).join("\n").trimEnd();

function fedInPieces(input, size) {
  const emu = new Emulator(24, 80);
  for (let i = 0; i < input.length; i += size) emu.write(input.slice(i, i + size));
  return emu;
}

test("a charset designation is consumed, not printed", () => {
  // `tput sgr0` for xterm-256color is ESC ( B ESC [ m. The B used to land on
  // screen after every coloured prompt.
  const emu = new Emulator(4, 40);
  emu.write("\x1b(B\x1b[mhello\x1b)0\x1b#8");
  assert.equal(screenText(emu), "hello");
});

test("DEC line drawing comes out as box characters", () => {
  const emu = new Emulator(4, 40);
  emu.write("\x1b(0lqqk\x1b(B ok");
  assert.equal(screenText(emu), "┌──┐ ok");
});

test("an escape sequence cut at every possible point parses the same", () => {
  const input = "\x1b[1;31mred\x1b[0m \x1b(Bplain\x1b]0;title\x07 \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ done";
  const whole = new Emulator(24, 80);
  whole.write(input);
  for (let size = 1; size <= 7; size++) {
    const emu = fedInPieces(input, size);
    assert.equal(screenText(emu), screenText(whole), `chunks of ${size}`);
    assert.equal(screenText(emu), "red plain link done");
  }
});

test("an OSC longer than one chunk is swallowed whole, however it is split", () => {
  // The old parser gave up on an unterminated OSC after 4 KB and printed the
  // rest of its payload — a long window title, an OSC 52 clipboard blob.
  const payload = "A".repeat(10_000);
  for (const [start, end] of [["\x1b]0;", "\x07"], ["\x1b]52;c;", "\x1b\\"]]) {
    const emu = new Emulator(4, 40);
    emu.write(start + payload.slice(0, 6000));
    emu.write(payload.slice(6000) + end.slice(0, 1));
    emu.write(end.slice(1) + "after");
    assert.equal(screenText(emu), "after");
  }
});

test("a DCS string is swallowed across chunks too", () => {
  const emu = new Emulator(4, 40);
  emu.write("\x1bPq#0;2;0;0;0");
  emu.write("#1~~~~\x1b");
  emu.write("\\visible");
  assert.equal(screenText(emu), "visible");
});

test("huge repeat counts neither hang nor allocate", () => {
  // Reachable from any MCP tool result: renderAnsiBlock runs this emulator on
  // it. `ESC[999999999L` was a billion-iteration loop, `ESC[2147483647@` an
  // Array of two billion blanks.
  const emu = new Emulator(24, 80);
  emu.write("hello");
  const started = Date.now();
  for (const final of ["@", "L", "M", "P", "X", "A", "B", "C", "D", "E", "F", "G", "d", "S", "T"]) {
    emu.write(`\x1b[2147483647${final}\x1b[99999999999999999999999${final}`);
  }
  assert.ok(Date.now() - started < 500, "a repeat count stalled the parser");
  assert.equal(emu.screen.length, 24);
  for (const line of emu.screen) assert.ok(line.chars.length <= 80);
});

test("a control sequence that never ends does not grow without bound", () => {
  const emu = new Emulator(4, 40);
  const digits = "1".repeat(100_000);
  for (let i = 0; i < 20; i++) emu.write("\x1b[" + digits);
  assert.ok(emu.pending.length < 1024, `pending grew to ${emu.pending.length}`);
  emu.write("m");
  emu.write("\r\x1b[2Kstill works");
  assert.equal(screenText(emu), "still works");
});

test("the cursor stays on the screen whatever it is told", () => {
  const emu = new Emulator(10, 20);
  emu.write("\x1b[9999;9999Hx\x1b[0;0Hy\x1b[-5;-5Hz");
  emu.write("\x1b7");
  emu.resize(5, 10);
  emu.write("\x1b8w");
  assert.ok(emu.row >= 0 && emu.row < 5 && emu.col >= 0 && emu.col <= 10);
  assert.equal(emu.screen.length, 5);
});

test("scrollback is capped, and so is what waits for the next paint", () => {
  // newScrollback is drained by requestAnimationFrame, which a hidden tab
  // never runs: `yes` in a background window grew it forever.
  const emu = new Emulator(24, 80);
  const chunk = "y\n".repeat(1000);
  for (let i = 0; i < 30; i++) emu.write(chunk);
  assert.ok(emu.scrollback.length <= MAX_SCROLLBACK);
  assert.ok(emu.newScrollback.length <= MAX_SCROLLBACK);
  assert.ok(emu.newScrollback.length <= emu.scrollback.length);
  // A painter that appends newScrollback and then drops `trimmed` lines from
  // the front must end up holding exactly the scrollback.
  assert.ok(emu.trimmed >= 0);
});

test("the painter's arithmetic holds across a paint", () => {
  const emu = new Emulator(4, 20);
  const dom = [];
  const paint = () => {
    dom.push(...emu.newScrollback);
    emu.newScrollback.length = 0;
    dom.splice(0, Math.min(emu.trimmed, dom.length));
    emu.trimmed = 0;
  };
  for (let round = 0; round < 5; round++) {
    for (let i = 0; i < 1500; i++) emu.write(`line ${round}-${i}\r\n`);
    paint();
    assert.deepEqual(dom.map(text), emu.scrollback.map(text));
  }
  for (let i = 0; i < 9000; i++) emu.write(`unpainted ${i}\r\n`);
  paint();
  assert.deepEqual(dom.map(text), emu.scrollback.map(text));
});

test("resizing inside the alternate screen resizes the screen it returns to", () => {
  const emu = new Emulator(24, 80);
  emu.write("prompt$ ");
  emu.write("\x1b[?1049h");
  emu.resize(10, 40);
  emu.write("\x1b[?1049l");
  assert.equal(emu.screen.length, 10);
  for (let i = 0; i < 30; i++) emu.write("\r\n");
  assert.equal(emu.screen.length, 10);
});

test("a full reset tells the painter the history went with it", () => {
  const emu = new Emulator(4, 20);
  for (let i = 0; i < 10; i++) emu.write(`line ${i}\r\n`);
  emu.write("\x1bc");
  assert.equal(emu.clearedHistory, true);
  assert.equal(emu.scrollback.length, 0);
});

test("reverse index at the top scrolls the screen down", () => {
  const emu = new Emulator(3, 10);
  emu.write("one\r\ntwo\r\nthree\x1b[H\x1bMzero");
  assert.deepEqual(emu.screen.map(text), ["zero", "one", "two"]);
});

test("C1 controls are not drawn as characters", () => {
  const emu = new Emulator(2, 20);
  emu.write("a\u0085b\u009bc\u0090d");
  assert.equal(screenText(emu), "abcd");
});

test("insert characters never widen a line past the screen", () => {
  const emu = new Emulator(2, 10);
  emu.write("0123456789\x1b[1;3H\x1b[4@");
  assert.equal(emu.screen[0].chars.length, 10);
  assert.equal(text(emu.screen[0]), "01    2345");
});

test("an emoji is never split across a wrap", () => {
  const emu = new Emulator(3, 5);
  emu.write("abcd😀e");
  const joined = emu.screen.map(text).join("|");
  assert.ok(joined.includes("😀"), joined);
  assert.ok(!/[\ud800-\udbff](?![\udc00-\udfff])/.test(joined), "lone high surrogate");
});

test("bracketed paste and application cursor modes are tracked", () => {
  const emu = new Emulator(2, 20);
  assert.equal(emu.bracketedPaste, false);
  emu.write("\x1b[?2004h\x1b[?1h");
  assert.equal(emu.bracketedPaste, true);
  assert.equal(emu.appCursor, true);
  emu.write("\x1b[?2004l\x1b[?1l");
  assert.equal(emu.bracketedPaste, false);
  assert.equal(emu.appCursor, false);
});

test("terminal output is text, never markup", () => {
  // Everything drawn goes through innerHTML; a tool result is attacker-shaped.
  const emu = new Emulator(2, 120);
  emu.write('\x1b[38;2;1;2;3m<img src=x onerror="alert(1)">&amp;\x1b[0m');
  const html = lineHtml(emu.screen[0]);
  assert.ok(!html.includes("<img"), html);
  assert.ok(html.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;amp;"), html);
  const block = renderAnsiBlock('\x1b[31m<script>alert(1)</script>\x1b[0m\r\n"x"');
  assert.ok(!block.includes("<script>"), block);
});

test("colour parameters cannot smuggle anything into a style attribute", () => {
  const emu = new Emulator(2, 40);
  emu.write("\x1b[38;2;999999;99999999999999999999;1000000m\x1b[48;5;99999mX");
  const html = lineHtml(emu.screen[0]);
  assert.match(html, /^<span style="[a-z0-9:;,.()% -]*">X<\/span>$/);
  assert.ok(html.includes("color:rgb(255,255,255)"), html);
});

test("renderAnsiBlock survives what an MCP server might send", () => {
  const nasty = "\x1b[?1049h\x1b[2147483647L\x1b]" + "x".repeat(50_000) + "\x1b[99999999@ok";
  const started = Date.now();
  const html = renderAnsiBlock(nasty);
  assert.ok(Date.now() - started < 500);
  assert.equal(typeof html, "string");
});

test("renderAnsiBlock can keep only the newest lines, for a live tail", () => {
  const text = Array.from({ length: 30 }, (_, i) => `\x1b[32mline ${i}\x1b[0m`).join("\n");
  const html = renderAnsiBlock(text, 80, 5);
  assert.equal(html.match(/class="qt-line"/g).length, 5);
  assert.ok(html.includes("line 25") && html.includes("line 29") && !html.includes("line 24"), html);
  assert.equal(renderAnsiBlock(text).match(/class="qt-line"/g).length, 30);
});
