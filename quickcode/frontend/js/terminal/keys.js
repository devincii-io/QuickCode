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

/**
 * What to send for one key press, or `null` for "not ours — let the browser
 * have it" (copy, paste, the panel's own shortcuts).
 */
export function keyToBytes(e) {
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
    return NAMED[key];
  }

  if (key.length === 1) return altKey ? "\x1b" + key : key;
  return null;
}
