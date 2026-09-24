// Keystrokes to the bytes a shell expects.
//
// The browser hands out `KeyboardEvent`s; a pty wants the wire encoding a
// terminal would have sent. Nothing here is clever, but all of it is
// load-bearing: without the arrow sequences there is no history, without
// `\x7f` backspace prints `^?`, and without `\x03` there is no way to stop a
// runaway command from inside the panel.

const CSI = "\x1b[";
const SS3 = "\x1bO";

const NAMED = {
  Enter: "\r",
  Tab: "\t",
  // The DEL character, not BS: that is what a terminal in its normal mode
  // sends, and readline treats the two differently.
  Backspace: "\x7f",
  Escape: "\x1b",
  ArrowUp: CSI + "A",
  ArrowDown: CSI + "B",
  ArrowRight: CSI + "C",
  ArrowLeft: CSI + "D",
  Home: CSI + "H",
  End: CSI + "F",
  Insert: CSI + "2~",
  Delete: CSI + "3~",
  PageUp: CSI + "5~",
  PageDown: CSI + "6~",
  F1: SS3 + "P", F2: SS3 + "Q", F3: SS3 + "R", F4: SS3 + "S",
  F5: CSI + "15~", F6: CSI + "17~", F7: CSI + "18~", F8: CSI + "19~",
  F9: CSI + "20~", F10: CSI + "21~", F11: CSI + "23~", F12: CSI + "24~",
};

// In application cursor mode (DECCKM, which vim and anything using ncurses'
// keypad() switch on) these arrive as SS3 sequences, and ncurses only maps
// the spelling its terminfo names — ESC [ A there is not an arrow.
const APP_CURSOR = {
  ArrowUp: SS3 + "A", ArrowDown: SS3 + "B", ArrowRight: SS3 + "C", ArrowLeft: SS3 + "D",
  Home: SS3 + "H", End: SS3 + "F",
};

const PASTE_START = "\x1b[200~";
const PASTE_END = "\x1b[201~";

/**
 * What to send for one key press, or `null` for "not ours — let the browser
 * have it" (copy, paste, the panel's own shortcuts). `modes` carries the
 * emulator's current input modes.
 */
export function keyToBytes(e, modes = {}) {
  const { key, ctrlKey, altKey, metaKey } = e;

  // Copy and paste stay with the browser. Ctrl+C is the interesting one: in a
  // terminal it interrupts, but with a selection on screen the user almost
  // certainly meant copy — so the selection decides, which is the same rule
  // Windows Terminal uses.
  if (ctrlKey && !altKey && (key === "c" || key === "C")) {
    const sel = String(window.getSelection() || "");
    if (sel) return null;
    return "\x03";
  }
  if (ctrlKey && (key === "v" || key === "V")) return null;   // paste event follows
  if (metaKey) return null;

  if (ctrlKey && !altKey && key.length === 1) {
    const code = key.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);  // ^@ … ^_
    if (key === "?") return "\x7f";
    return null;
  }

  if (NAMED[key] !== undefined) {
    // Alt+arrow is word-wise movement in readline.
    if (altKey && key === "ArrowLeft") return "\x1bb";
    if (altKey && key === "ArrowRight") return "\x1bf";
    if (modes.appCursor && APP_CURSOR[key]) return APP_CURSOR[key];
    return NAMED[key];
  }

  if (key.length === 1) return altKey ? "\x1b" + key : key;
  return null;
}

/**
 * A paste, as the shell should receive it. Newlines become Enter, as typed.
 * When the shell asked for bracketed paste (the emulator tracks ?2004) the
 * text is marked, so readline inserts it instead of running each line — and
 * an end marker inside the clipboard, which would close the paste early and
 * run the remainder as keystrokes, is removed until none is left.
 */
export function pasteBytes(text, bracketed) {
  let body = String(text).replace(/\r\n/g, "\r").replace(/\n/g, "\r");
  if (!bracketed) return body;
  while (body.includes(PASTE_END)) body = body.split(PASTE_END).join("");
  return PASTE_START + body + PASTE_END;
}

/**
 * The model's command, made safe to put at the user's prompt.
 *
 * The contract of "run here" is that a human keystroke runs it. Stripping
 * newlines did not keep it: readline also executes on ^O and after ^X^E, and
 * an escape sequence can end a bracketed paste. So every C0 and C1 control,
 * and DEL, is replaced; what is left is text and nothing but text.
 */
export function stagedCommand(command) {
  // eslint-disable-next-line no-control-regex
  return String(command ?? "").replace(/[\x00-\x1f\x7f-\x9f]+/g, " ").trim();
}

/**
 * `text` in pieces of at most `size` UTF-16 units, never splitting a
 * surrogate pair. The server takes a bounded amount per frame
 * (server/terminal.py MAX_INPUT_CHARS) and used to cut a large paste short.
 */
export function inputChunks(text, size) {
  const out = [];
  let i = 0;
  while (i < text.length) {
    let end = Math.min(i + size, text.length);
    const code = text.charCodeAt(end - 1);
    if (end < text.length && code >= 0xd800 && code <= 0xdbff && end - 1 > i) end--;
    out.push(text.slice(i, end));
    i = end;
  }
  return out;
}
