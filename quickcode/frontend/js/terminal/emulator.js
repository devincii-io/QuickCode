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

export const MAX_SCROLLBACK = 4000;

// The 16 ANSI colours resolve to CSS variables so a QuickCode theme can own
// them (see css/terminal.css); 16-255 are the xterm cube and greyscale, which
// no theme has an opinion about, so they are computed.
const ANSI_VAR = (i) => `var(--qt-a${i})`;
const CUBE = [0, 95, 135, 175, 215, 255];

function xterm256(n) {
  if (n < 16) return ANSI_VAR(n);
  if (n < 232) {
    const i = n - 16;
    return `rgb(${CUBE[Math.floor(i / 36) % 6]},${CUBE[Math.floor(i / 6) % 6]},${CUBE[i % 6]})`;
  }
  const v = 8 + (n - 232) * 10;
  return `rgb(${v},${v},${v})`;
}

export const DEFAULT_STYLE = Object.freeze({
  fg: null, bg: null, bold: false, dim: false, italic: false,
  underline: false, inverse: false, hidden: false, strike: false,
});

function blankLine() {
  return { chars: [], attrs: [], dirty: true };
}

export class Emulator {
  constructor(rows = 24, cols = 80) {
    this.rows = Math.max(1, rows);
    this.cols = Math.max(1, cols);
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
    this.altScreen = null;     // the main screen, parked, while alt is active
    this.allDirty = true;
  }

  // ---- geometry ----

  resize(rows, cols) {
    rows = Math.max(1, rows);
    cols = Math.max(1, cols);
    if (rows === this.rows && cols === this.cols) return;
    // Growing keeps what is on screen; shrinking pushes the top rows into
    // scrollback rather than deleting them, which is what a real terminal does
    // and what stops a drag-resize from eating the last command's output.
    while (this.screen.length > rows) {
      if (this.row > 0) { this.pushScroll(this.screen.shift()); this.row--; }
      else this.screen.pop();
    }
    while (this.screen.length < rows) this.screen.push(blankLine());
    this.rows = rows;
    this.cols = cols;
    this.row = Math.min(this.row, rows - 1);
    this.col = Math.min(this.col, cols - 1);
    this.allDirty = true;
  }

  pushScroll(line) {
    // The alternate screen is by definition not history: `less` scrolling its
    // page must not deposit forty copies of the file into the scrollback.
    if (this.altScreen) return;
    this.scrollback.push(line);
    this.newScrollback.push(line);
    while (this.scrollback.length > MAX_SCROLLBACK) {
      this.scrollback.shift();
      this.trimmed++;
    }
  }

  // ---- writing ----

  write(text) {
    const s = this.pending + text;
    this.pending = "";
    let i = 0;
    while (i < s.length) {
      const ch = s[i];
      if (ch === "\x1b") {
        const consumed = this.escape(s, i);
        if (consumed < 0) { this.pending = s.slice(i); return; }  // incomplete
        i += consumed;
        continue;
      }
      i++;
      switch (ch) {
        case "\n": this.lineFeed(); break;
        case "\r": this.col = 0; break;
        case "\b": this.col = Math.max(0, this.col - 1); break;
        case "\t": this.col = Math.min(this.cols - 1, (this.col + 8) & ~7); break;
        case "\x07": break;                                       // bell
        case "\x0b": case "\x0c": this.lineFeed(); break;
        default:
          if (ch >= " " && ch !== "\x7f") this.put(ch);
          break;
      }
    }
  }

  put(ch) {
    if (this.col >= this.cols) { this.col = 0; this.lineFeed(); }
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
    this.pushScroll(this.screen.shift());
    this.screen.push(blankLine());
    this.allDirty = true;
  }

  // ---- escape sequences ----

  /** Returns how many characters were consumed, or -1 if the sequence is cut off. */
  escape(s, start) {
    const next = s[start + 1];
    if (next === undefined) return -1;
    if (next === "[") return this.csi(s, start);
    if (next === "]") return this.osc(s, start);
    if (next === "P" || next === "^" || next === "_") return this.stString(s, start);
    // Two-character escapes. Only the handful a shell actually emits matter.
    if (next === "7") { this.saved = { row: this.row, col: this.col, style: this.style }; return 2; }
    if (next === "8") { this.restore(); return 2; }
    if (next === "M") { this.row = Math.max(0, this.row - 1); return 2; }
    if (next === "c") { this.reset(); return 2; }
    return 2;
  }

  csi(s, start) {
    // ESC [ <params> <intermediates> <final>
    let i = start + 2;
    let params = "";
    while (i < s.length && s[i] >= "\x30" && s[i] <= "\x3f") params += s[i++];
    // Intermediates are skipped rather than kept: no sequence that carries one
    // draws anything a shell panel has to show.
    while (i < s.length && s[i] >= "\x20" && s[i] <= "\x2f") i++;
    if (i >= s.length) return -1;
    const final = s[i++];
    this.applyCsi(params, final);
    return i - start;
  }

  osc(s, start) {
    // OSC ... BEL, or OSC ... ESC \. Window titles, mostly; nothing to draw.
    const bel = s.indexOf("\x07", start + 2);
    const st = s.indexOf("\x1b\\", start + 2);
    if (bel < 0 && st < 0) return s.length - start > 4096 ? s.length - start : -1;
    if (bel >= 0 && (st < 0 || bel < st)) return bel - start + 1;
    return st - start + 2;
  }

  stString(s, start) {
    const st = s.indexOf("\x1b\\", start + 2);
    if (st < 0) return s.length - start > 4096 ? s.length - start : -1;
    return st - start + 2;
  }

  applyCsi(params, final) {
    const priv = params.startsWith("?");
    const nums = (priv ? params.slice(1) : params)
      .split(";").map((p) => (p === "" ? 0 : parseInt(p, 10) || 0));
    const n = nums[0] || 0;
    const n1 = nums[0] === 0 ? 1 : nums[0];

    if (priv) { this.decMode(nums, final); return; }

    switch (final) {
      case "m": this.sgr(params === "" ? [0] : nums); break;
      case "A": this.row = Math.max(0, this.row - n1); break;
      case "B": this.row = Math.min(this.rows - 1, this.row + n1); break;
      case "C": this.col = Math.min(this.cols - 1, this.col + n1); break;
      case "D": this.col = Math.max(0, this.col - n1); break;
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
      case "s": this.saved = { row: this.row, col: this.col, style: this.style }; break;
      case "u": this.restore(); break;
      default: break;   // scroll regions, device reports, mouse — not our job
    }
  }

  decMode(nums, final) {
    // The alternate screen is the one private mode worth modelling: without it
    // every `less`, `vim` or `top` leaves its whole redraw in the scrollback.
    if (!nums.includes(1049) && !nums.includes(47) && !nums.includes(1047)) return;
    if (final === "h" && !this.altScreen) {
      this.altScreen = { screen: this.screen, row: this.row, col: this.col };
      this.screen = Array.from({ length: this.rows }, blankLine);
      this.row = 0; this.col = 0;
      this.allDirty = true;
    } else if (final === "l" && this.altScreen) {
      this.screen = this.altScreen.screen;
      this.row = Math.min(this.altScreen.row, this.rows - 1);
      this.col = this.altScreen.col;
      this.altScreen = null;
      while (this.screen.length < this.rows) this.screen.push(blankLine());
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
    const pad = Array.from({ length: count }, () => " ");
    line.chars.splice(this.col, 0, ...pad);
    line.attrs.splice(this.col, 0, ...pad.map(() => this.style));
    line.dirty = true;
  }

  insertLines(count) {
    for (let i = 0; i < count; i++) {
      this.screen.splice(this.row, 0, blankLine());
      this.screen.pop();
    }
    this.allDirty = true;
  }

  deleteLines(count) {
    for (let i = 0; i < count; i++) {
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
          st = { ...st, [key]: `rgb(${nums[i + 2] || 0},${nums[i + 3] || 0},${nums[i + 4] || 0})` };
          i += 4;
        }
      }
    }
    this.style = st;
  }
}

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
 */
export function renderAnsiBlock(text, cols = 200) {
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
  return lines.map((line) => `<div class="qt-line">${lineHtml(line) || "&nbsp;"}</div>`).join("");
}
