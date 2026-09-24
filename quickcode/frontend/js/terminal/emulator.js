// A small terminal emulator: bytes in, a screen model out. No DOM here.
//
// Why write one at all. The panel needed ANSI *rendering*, and the only ANSI
// code in the repo was `_ANSI_RE` in tools/bash.py, which does the opposite —
// it strips escapes so the model sees clean text. Stripping is wrong here
// twice over: colour is half of why a terminal is worth having, and `\r` is
// not decoration. A progress bar redraws its line by returning to column zero
// and overwriting; strip the control codes and you get forty copies of the
// same line instead of one that counts up.
//
// Rejected: xterm.js. It is the right library and it is 300 KB of bundled
// dependency — QuickCode has no build step and must work offline, so a CDN
// tag is out and vendoring a bundle for one panel is out of proportion. What a
// shell panel actually needs is a screen, a cursor, SGR colour, the erase
// codes and the alternate screen; that is this file.
//
// The model is a real viewport rather than a growing list of lines, because
// programs address the screen absolutely (`ESC[2;1H` means row 2 of the
// *screen*, not line 2 of the session). So: `screen` is exactly `rows` lines,
// `scrollback` holds the lines that have scrolled off the top, and cursor
// coordinates are screen-relative — which is what makes clear-screen, CUP and
// `less` come out right instead of approximately right.
//
// The input is hostile by assumption. Besides the pty, `renderAnsiBlock` runs
// this on tool results from MCP servers and plugins, so every repeat count is
// clamped to the screen, every sequence has a length limit, and a string
// (OSC, DCS) is discarded as it streams past rather than buffered until its
// terminator shows up.

export const MAX_SCROLLBACK = 4000;
// History is trimmed this many lines at a time, not one per line scrolled:
// shifting a four-thousand-entry array for every line of `yes` is quadratic.
const TRIM_BATCH = 256;
// The longest escape sequence held while waiting for the rest of it. Real
// ones are a few dozen characters; past this it is discarded as it arrives.
const MAX_SEQUENCE = 256;
// No repeat count means more than this; xterm caps its parameters the same way.
const MAX_PARAM = 0xffff;

// The 16 ANSI colours resolve to CSS variables so a QuickCode theme can own
// them (see css/terminal.css); 16-255 are the xterm cube and greyscale, which
// no theme has an opinion about, so they are computed.
const ANSI_VAR = (i) => `var(--qt-a${i})`;
const CUBE = [0, 95, 135, 175, 215, 255];

function xterm256(n) {
  n = clamp(n, 0, 255);
  if (n < 16) return ANSI_VAR(n);
  if (n < 232) {
    const i = n - 16;
    return `rgb(${CUBE[Math.floor(i / 36) % 6]},${CUBE[Math.floor(i / 6) % 6]},${CUBE[i % 6]})`;
  }
  const v = 8 + (n - 232) * 10;
  return `rgb(${v},${v},${v})`;
}

// DEC Special Graphics, selected by `ESC ( 0`: what ncurses draws boxes with
// when it does not trust the locale (`smacs` in the xterm terminfo).
const LINE_DRAWING = {
  "`": "◆", a: "▒", f: "°", g: "±", j: "┘", k: "┐", l: "┌", m: "└", n: "┼",
  o: "⎺", p: "⎻", q: "─", r: "⎼", s: "⎽", t: "├", u: "┤", v: "┴", w: "┬",
  x: "│", y: "≤", z: "≥", "{": "π", "|": "≠", "}": "£", "~": "·",
};

export const DEFAULT_STYLE = Object.freeze({
  fg: null, bg: null, bold: false, dim: false, italic: false,
  underline: false, inverse: false, hidden: false, strike: false,
});

function blankLine() {
  return { chars: [], attrs: [], dirty: true };
}

const isParam = (c) => c >= "\x30" && c <= "\x3f";
const isIntermediate = (c) => c >= "\x20" && c <= "\x2f";
const isFinal = (c) => c >= "\x40" && c <= "\x7e";

export class Emulator {
  constructor(rows = 24, cols = 80) {
    this.rows = dimension(rows);
    this.cols = dimension(cols);
    this.scrollback = [];
    this.screen = [];
    this.trimmed = 0;          // scrollback lines dropped since the last render
    this.reset();
  }

  reset() {
    this.screen = Array.from({ length: this.rows }, blankLine);
    this.scrollback = [];
    this.newScrollback = [];   // pushed off the top since the last render
    this.trimmed = 0;
    this.row = 0;
    this.col = 0;
    this.style = DEFAULT_STYLE;
    this.saved = null;
    this.pending = "";         // an escape sequence split across two chunks
    this.skip = null;          // discarding a string ("osc"/"st") or a bad CSI ("csi")
    this.skipEsc = false;      // ...and the last chunk ended on what may be its ST
    this.charset = null;       // G0: null for ASCII, LINE_DRAWING after ESC ( 0
    this.altScreen = null;     // the main screen, parked, while alt is active
    this.bracketedPaste = false;
    this.appCursor = false;
    this.allDirty = true;
  }

  // ---- geometry ----

  resize(rows, cols) {
    rows = dimension(rows);
    cols = dimension(cols);
    if (rows === this.rows && cols === this.cols) return;
    // Growing keeps what is on screen; shrinking pushes the top rows into
    // scrollback rather than deleting them, which is what a real terminal does
    // and what stops a drag-resize from eating the last command's output. The
    // main screen parked behind `less` is resized too, or leaving `less`
    // would restore a screen taller than the terminal.
    if (this.altScreen) {
      const main = this.altScreen;
      [main.screen, main.row] = this.fit(main.screen, main.row, rows, true);
      main.col = Math.min(main.col, cols - 1);
      [this.screen, this.row] = this.fit(this.screen, this.row, rows, false);
    } else {
      [this.screen, this.row] = this.fit(this.screen, this.row, rows, true);
    }
    this.rows = rows;
    this.cols = cols;
    this.col = Math.min(this.col, cols - 1);
    this.allDirty = true;
  }

  fit(screen, row, rows, history) {
    while (screen.length > rows) {
      if (row > 0) {
        const top = screen.shift();
        if (history) this.keep(top);
        row--;
      } else {
        screen.pop();
      }
    }
    while (screen.length < rows) screen.push(blankLine());
    return [screen, Math.min(row, rows - 1)];
  }

  pushScroll(line) {
    // The alternate screen is by definition not history: `less` scrolling its
    // page must not deposit forty copies of the file into the scrollback.
    if (this.altScreen) return;
    this.keep(line);
  }

  keep(line) {
    this.scrollback.push(line);
    this.newScrollback.push(line);
    if (this.scrollback.length <= MAX_SCROLLBACK) return;
    const drop = this.scrollback.length - (MAX_SCROLLBACK - TRIM_BATCH);
    this.scrollback.splice(0, drop);
    this.trimmed += drop;
    // Lines that left the ring before any paint saw them never need to reach
    // the DOM. Without this a hidden tab, where requestAnimationFrame never
    // fires, grew newScrollback for as long as the program kept printing.
    const stale = this.newScrollback.length - this.scrollback.length;
    if (stale > 0) {
      this.newScrollback.splice(0, stale);
      this.trimmed -= stale;
    }
  }

  // ---- writing ----

  write(text) {
    const s = this.pending + text;
    this.pending = "";
    let i = 0;
    while (i < s.length) {
      if (this.skip) {
        const end = this.skipRest(s, i);
        if (end < 0) return;
        i = end;
        continue;
      }
      const ch = s[i];
      if (ch === "\x1b") {
        const consumed = this.escape(s, i);
        if (consumed < 0) {
          this.pending = s.slice(i, i + MAX_SEQUENCE);
          return;
        }
        i += consumed;
        continue;
      }
      i++;
      const code = ch.charCodeAt(0);
      if (code < 0x20 || code === 0x7f) { this.control(ch); continue; }
      if (code >= 0x80 && code <= 0x9f) continue;       // C1 controls draw nothing
      if (code >= 0xd800 && code <= 0xdbff && i < s.length) {
        const low = s.charCodeAt(i);
        if (low >= 0xdc00 && low <= 0xdfff) {
          this.putWide(ch + s[i]);
          i++;
          continue;
        }
      }
      this.put((this.charset && this.charset[ch]) || ch);
    }
  }

  control(ch) {
    switch (ch) {
      case "\n": case "\x0b": case "\x0c": this.lineFeed(); break;
      case "\r": this.col = 0; break;
      case "\b": this.col = Math.max(0, this.col - 1); break;
      case "\t": this.col = Math.min(this.cols - 1, (this.col + 8) & ~7); break;
      default: break;                                   // BEL, SO/SI, the rest
    }
  }

  put(ch) {
    if (this.col >= this.cols) { this.col = 0; this.lineFeed(); }
    this.cell(ch);
  }

  /** A character outside the BMP: emoji, mostly, which a terminal draws two
   *  cells wide. Kept in one cell plus a blank spacer so a wrap cannot split
   *  the surrogate pair across two lines. */
  putWide(pair) {
    if (this.cols < 2) { this.put(pair); return; }
    if (this.col >= this.cols - 1) { this.col = 0; this.lineFeed(); }
    this.cell(pair);
    this.cell("");
  }

  cell(ch) {
    const line = this.screen[this.row];
    while (line.chars.length < this.col) {
      line.chars.push(" ");
      line.attrs.push(DEFAULT_STYLE);
    }
    line.chars[this.col] = ch;
    line.attrs[this.col] = this.style;
    line.dirty = true;
    this.col++;
  }

  lineFeed() {
    this.row++;
    if (this.row < this.rows) return;
    this.row = this.rows - 1;
    this.scrollUp(1);
  }

  scrollUp(count) {
    for (let i = 0; i < Math.min(count, this.rows); i++) {
      this.pushScroll(this.screen.shift());
      this.screen.push(blankLine());
    }
    this.allDirty = true;
  }

  scrollDown(count) {
    for (let i = 0; i < Math.min(count, this.rows); i++) {
      this.screen.pop();
      this.screen.unshift(blankLine());
    }
    this.allDirty = true;
  }

  reverseIndex() {
    if (this.row > 0) this.row--;
    else this.scrollDown(1);
  }

  // ---- escape sequences ----

  /** Returns how many characters were consumed, or -1 if the sequence is cut off. */
  escape(s, start) {
    const next = s[start + 1];
    if (next === undefined) return -1;
    switch (next) {
      case "[": return this.csi(s, start);
      case "]": return this.string(s, start, "osc");
      case "P": case "X": case "^": case "_": return this.string(s, start, "st");
      case "7": this.saved = { row: this.row, col: this.col, style: this.style }; return 2;
      case "8": this.restore(); return 2;
      case "D": this.lineFeed(); return 2;
      case "E": this.col = 0; this.lineFeed(); return 2;
      case "M": this.reverseIndex(); return 2;
      case "c":
        // A full reset takes the history with it; the painter has to hear so.
        this.reset();
        this.clearedHistory = true;
        return 2;
      default: break;
    }
    if (isIntermediate(next)) {
      // ESC, intermediates, one final: `ESC ( B` (ASCII), `ESC ( 0` (line
      // drawing), `ESC # 8`. Consumed whole — `tput sgr0` ends in ESC ( B,
      // and treating it as a two-character escape printed the B.
      let i = start + 1;
      while (i < s.length && i - start < MAX_SEQUENCE && isIntermediate(s[i])) i++;
      if (i >= s.length) return -1;
      if (next === "(") this.charset = s[i] === "0" ? LINE_DRAWING : null;
      return i - start + 1;
    }
    // ESC before a control, or before another ESC: the first one was
    // abandoned. Drop it and let what follows be itself.
    if (next < "\x20") return 1;
    return 2;
  }

  csi(s, start) {
    // ESC [ <params> <intermediates> <final>
    const limit = start + MAX_SEQUENCE;
    let i = start + 2;
    while (i < s.length && i < limit && isParam(s[i])) i++;
    const paramEnd = i;
    while (i < s.length && i < limit && isIntermediate(s[i])) i++;
    if (i >= limit || (i < s.length && isParam(s[i]))) {
      // Overlong, or malformed (a parameter after an intermediate): discard
      // through the final byte, however many chunks away that is.
      this.skip = "csi";
      return i - start;
    }
    if (i >= s.length) return -1;
    const final = s[i];
    if (!isFinal(final)) return i - start;   // a control mid-sequence: abandon, run it
    if (paramEnd === i) this.applyCsi(s.slice(start + 2, paramEnd), final);
    return i - start + 1;
  }

  string(s, start, kind) {
    // OSC (titles, hyperlinks, clipboard) and DCS/SOS/PM/APC: nothing a panel
    // draws. Discarded as they stream by, never held until the terminator.
    this.skip = kind;
    const end = this.skipRest(s, start + 2);
    return end < 0 ? s.length - start : end - start;
  }

  /** Discard the rest of a string or bad CSI from `i`; the index after it, or -1. */
  skipRest(s, i) {
    if (this.skip === "csi") {
      for (; i < s.length; i++) {
        if (isParam(s[i]) || isIntermediate(s[i])) continue;
        this.skip = null;
        return isFinal(s[i]) ? i + 1 : i;
      }
      return -1;
    }
    if (this.skipEsc) {
      this.skipEsc = false;
      this.skip = null;
      return s[i] === "\\" ? i + 1 : i;
    }
    for (; i < s.length; i++) {
      const c = s[i];
      if ((c === "\x07" && this.skip === "osc") || c === "\x18" || c === "\x1a") {
        this.skip = null;
        return i + 1;
      }
      if (c === "\x1b") {
        if (i + 1 >= s.length) { this.skipEsc = true; return -1; }
        this.skip = null;
        return s[i + 1] === "\\" ? i + 2 : i;
      }
    }
    return -1;
  }

  applyCsi(params, final) {
    if (/^[<=>]/.test(params)) return;       // private-use prefixes other than ?
    const priv = params.startsWith("?");
    const nums = (priv ? params.slice(1) : params)
      .split(";").map((p) => Math.min(parseInt(p, 10) || 0, MAX_PARAM));
    const n = nums[0] || 0;
    const n1 = nums[0] === 0 ? 1 : nums[0];

    if (priv) { this.decMode(nums, final); return; }

    switch (final) {
      case "m": this.sgr(params === "" ? [0] : nums); break;
      case "A": this.row = Math.max(0, this.row - n1); break;
      case "B": this.row = Math.min(this.rows - 1, this.row + n1); break;
      case "C": this.col = Math.min(this.cols - 1, this.col + n1); break;
      case "D": this.col = Math.max(0, Math.min(this.col, this.cols) - n1); break;
      case "E": this.row = Math.min(this.rows - 1, this.row + n1); this.col = 0; break;
      case "F": this.row = Math.max(0, this.row - n1); this.col = 0; break;
      case "G": case "`": this.col = clamp(n1 - 1, 0, this.cols - 1); break;
      case "d": this.row = clamp(n1 - 1, 0, this.rows - 1); break;
      case "H": case "f":
        this.row = clamp((nums[0] || 1) - 1, 0, this.rows - 1);
        this.col = clamp((nums[1] || 1) - 1, 0, this.cols - 1);
        break;
      case "J": this.eraseDisplay(n); break;
      case "K": this.eraseLine(n); break;
      case "L": this.insertLines(n1); break;
      case "M": this.deleteLines(n1); break;
      case "P": this.deleteChars(n1); break;
      case "@": this.insertChars(n1); break;
      case "X": this.eraseChars(n1); break;
      case "S": this.scrollUp(n1); break;
      case "T": this.scrollDown(n1); break;
      case "s": this.saved = { row: this.row, col: this.col, style: this.style }; break;
      case "u": this.restore(); break;
      default: break;   // scroll regions, device reports, mouse — not our job
    }
  }

  decMode(nums, final) {
    if (final !== "h" && final !== "l") return;
    const on = final === "h";
    for (const mode of nums) {
      // Application cursor keys: vim, htop and anything using ncurses'
      // keypad() then expect ESC O A for an arrow, not ESC [ A (keys.js).
      if (mode === 1) this.appCursor = on;
      // Bracketed paste: the shell wants pastes marked so it can insert them
      // as text instead of running each line as it arrives (panel.js).
      else if (mode === 2004) this.bracketedPaste = on;
      // The alternate screen is the one screen mode worth modelling: without
      // it every `less`, `vim` or `top` leaves its whole redraw in history.
      else if (mode === 1049 || mode === 47 || mode === 1047) this.alternate(on);
    }
  }

  alternate(on) {
    if (on && !this.altScreen) {
      this.altScreen = { screen: this.screen, row: this.row, col: this.col };
      this.screen = Array.from({ length: this.rows }, blankLine);
      this.row = 0; this.col = 0;
      this.allDirty = true;
    } else if (!on && this.altScreen) {
      this.screen = this.altScreen.screen;
      this.row = Math.min(this.altScreen.row, this.rows - 1);
      this.col = Math.min(this.altScreen.col, this.cols - 1);
      this.altScreen = null;
      this.allDirty = true;
    }
  }

  restore() {
    if (!this.saved) return;
    this.row = Math.min(this.saved.row, this.rows - 1);
    this.col = Math.min(this.saved.col, this.cols - 1);
    this.style = this.saved.style;
  }

  // ---- erasing ----

  eraseLine(mode) {
    const line = this.screen[this.row];
    if (mode === 0) { line.chars.length = Math.min(line.chars.length, this.col); line.attrs.length = line.chars.length; }
    else if (mode === 1) { for (let i = 0; i <= this.col && i < line.chars.length; i++) { line.chars[i] = " "; line.attrs[i] = this.style; } }
    else { line.chars.length = 0; line.attrs.length = 0; }
    line.dirty = true;
  }

  eraseDisplay(mode) {
    if (mode === 2 || mode === 3) {
      // Clear, not scroll: `clear` means "give me an empty screen", and a
      // terminal that pushed the old one into history would leave the user
      // scrolling through the thing they just asked to be rid of.
      this.screen = Array.from({ length: this.rows }, blankLine);
      this.row = 0; this.col = 0;
      if (mode === 3) { this.scrollback = []; this.newScrollback = []; this.trimmed = 0; this.clearedHistory = true; }
      this.allDirty = true;
      return;
    }
    if (mode === 0) {
      this.eraseLine(0);
      for (let r = this.row + 1; r < this.rows; r++) this.screen[r] = blankLine();
    } else {
      this.eraseLine(1);
      for (let r = 0; r < this.row; r++) this.screen[r] = blankLine();
    }
    this.allDirty = true;
  }

  eraseChars(count) {
    const line = this.screen[this.row];
    for (let i = this.col; i < this.col + count && i < line.chars.length; i++) {
      line.chars[i] = " ";
      line.attrs[i] = this.style;
    }
    line.dirty = true;
  }

  deleteChars(count) {
    const line = this.screen[this.row];
    line.chars.splice(this.col, count);
    line.attrs.splice(this.col, count);
    line.dirty = true;
  }

  insertChars(count) {
    const line = this.screen[this.row];
    count = Math.min(count, this.cols - this.col);
    if (count <= 0 || this.col >= line.chars.length) return;
    const pad = Array.from({ length: count }, () => " ");
    line.chars.splice(this.col, 0, ...pad);
    line.attrs.splice(this.col, 0, ...pad.map(() => this.style));
    // What is pushed past the right margin is gone, as on a real screen.
    line.chars.length = Math.min(line.chars.length, this.cols);
    line.attrs.length = line.chars.length;
    line.dirty = true;
  }

  insertLines(count) {
    for (let i = 0; i < Math.min(count, this.rows - this.row); i++) {
      this.screen.splice(this.row, 0, blankLine());
      this.screen.pop();
    }
    this.allDirty = true;
  }

  deleteLines(count) {
    for (let i = 0; i < Math.min(count, this.rows - this.row); i++) {
      this.screen.splice(this.row, 1);
      this.screen.push(blankLine());
    }
    this.allDirty = true;
  }

  // ---- SGR ----

  sgr(nums) {
    let st = this.style;
    for (let i = 0; i < nums.length; i++) {
      const p = nums[i];
      if (p === 0) { st = DEFAULT_STYLE; continue; }
      if (p === 1) { st = { ...st, bold: true }; continue; }
      if (p === 2) { st = { ...st, dim: true }; continue; }
      if (p === 3) { st = { ...st, italic: true }; continue; }
      if (p === 4) { st = { ...st, underline: true }; continue; }
      if (p === 7) { st = { ...st, inverse: true }; continue; }
      if (p === 8) { st = { ...st, hidden: true }; continue; }
      if (p === 9) { st = { ...st, strike: true }; continue; }
      if (p === 21 || p === 22) { st = { ...st, bold: false, dim: false }; continue; }
      if (p === 23) { st = { ...st, italic: false }; continue; }
      if (p === 24) { st = { ...st, underline: false }; continue; }
      if (p === 27) { st = { ...st, inverse: false }; continue; }
      if (p === 28) { st = { ...st, hidden: false }; continue; }
      if (p === 29) { st = { ...st, strike: false }; continue; }
      if (p >= 30 && p <= 37) { st = { ...st, fg: ANSI_VAR(p - 30) }; continue; }
      if (p >= 90 && p <= 97) { st = { ...st, fg: ANSI_VAR(p - 90 + 8) }; continue; }
      if (p >= 40 && p <= 47) { st = { ...st, bg: ANSI_VAR(p - 40) }; continue; }
      if (p >= 100 && p <= 107) { st = { ...st, bg: ANSI_VAR(p - 100 + 8) }; continue; }
      if (p === 39) { st = { ...st, fg: null }; continue; }
      if (p === 49) { st = { ...st, bg: null }; continue; }
      if (p === 38 || p === 48) {
        const key = p === 38 ? "fg" : "bg";
        if (nums[i + 1] === 5) { st = { ...st, [key]: xterm256(nums[i + 2] || 0) }; i += 2; }
        else if (nums[i + 1] === 2) {
          const [r, g, b] = [nums[i + 2], nums[i + 3], nums[i + 4]].map((v) => clamp(v || 0, 0, 255));
          st = { ...st, [key]: `rgb(${r},${g},${b})` };
          i += 4;
        }
      }
    }
    this.style = st;
  }
}

function dimension(n) { return clamp(Math.floor(Number(n)) || 1, 1, 1000); }


function clamp(v, lo, hi) { return Math.max(lo, Math.min(v, hi)); }

// ---- rendering one line to HTML ----

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ESCAPES[c]);

function css(style) {
  const fg = style.inverse ? (style.bg || "var(--qt-bg)") : style.fg;
  const bg = style.inverse ? (style.fg || "var(--qt-fg)") : style.bg;
  let out = "";
  if (fg) out += `color:${fg};`;
  if (bg) out += `background:${bg};`;
  if (style.bold) out += "font-weight:600;";
  if (style.dim) out += "opacity:.6;";
  if (style.italic) out += "font-style:italic;";
  if (style.underline) out += "text-decoration:underline;";
  if (style.strike) out += (style.underline ? "" : "text-decoration:") + "line-through;";
  if (style.hidden) out += "visibility:hidden;";
  return out;
}

/** One screen or scrollback line as HTML. Runs of one style become one span. */
export function lineHtml(line) {
  const { chars, attrs } = line;
  // Trailing blanks carry no information and would make every line the full
  // width of the viewport, which breaks selection and doubles the DOM.
  let end = chars.length;
  while (end > 0 && chars[end - 1] === " " && attrs[end - 1] === DEFAULT_STYLE) end--;
  if (end === 0) return "";
  let html = "";
  let runStart = 0;
  for (let i = 1; i <= end; i++) {
    if (i === end || attrs[i] !== attrs[runStart]) {
      const text = escapeHtml(chars.slice(runStart, i).join(""));
      const style = css(attrs[runStart] || DEFAULT_STYLE);
      html += style ? `<span style="${style}">${text}</span>` : text;
      runStart = i;
    }
  }
  return html;
}

/**
 * A finished block of terminal output as static HTML.
 *
 * The agent's `bash` results are a *record*, not a live session, but they are
 * still terminal output: a build log in them carries the same `\r` redraws and
 * the same colour codes. Running them through the same emulator is both less
 * code than a second renderer and more correct than one — a progress bar comes
 * out as the one line it finished on rather than four hundred.
 *
 * `maxLines` keeps only the newest lines, for a view that tails a live job.
 */
export function renderAnsiBlock(text, cols = 200, maxLines = Infinity) {
  const emu = new Emulator(1, cols);
  // A pty ends its lines CR+LF; a stored tool result has had that normalised
  // to a bare LF on the way to the model (tools/bash.py `_clean_pty_output`).
  // The emulator is right to treat LF as "down one row, same column" — that
  // is what a terminal does — so without putting the carriage return back,
  // every line of a saved log would start where the previous one ended and the
  // block would come out as a staircase. A lone CR is left alone: that is the
  // redraw this renderer exists to collapse.
  emu.write(String(text || "").replace(/\r\n/g, "\n").replace(/\n/g, "\r\n"));
  const lines = [...emu.scrollback, ...emu.screen];
  while (lines.length && !lines[lines.length - 1].chars.length) lines.pop();
  if (lines.length > maxLines) lines.splice(0, lines.length - maxLines);
  return lines.map((line) => `<div class="qt-line">${lineHtml(line) || "&nbsp;"}</div>`).join("");
}
