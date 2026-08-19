// The emulator's screen, on screen. Incremental, because a terminal that
// re-rendered four thousand lines per frame would make a build log feel like a
// stall.
//
// Two regions, for two lifetimes. Scrollback lines never change once they have
// scrolled off, so they are appended as elements and left alone. The viewport
// is exactly `rows` elements, re-rendered only where the emulator marked a line
// dirty. Writes arrive in bursts, so painting is deferred to the next animation
// frame: a hundred WebSocket frames in one tick cost one layout, not a hundred.

import { Emulator, lineHtml } from "./emulator.js";

export class TerminalView {
  constructor(host) {
    this.host = host;
    host.classList.add("qt-screen");
    host.innerHTML = `<div class="qt-scrollback"></div><div class="qt-viewport"></div>`;
    this.scrollEl = host.querySelector(".qt-scrollback");
    this.viewEl = host.querySelector(".qt-viewport");
    this.emu = new Emulator(24, 80);
    this.rendered = 0;      // scrollback lines already in the DOM
    this.frame = 0;
    this.rows = [];
    this.syncRows();
    // "Stick to the bottom" is a decision about intent, not position: someone
    // who scrolled up to read an error must not be yanked back down by the
    // next line of output.
    this.follow = true;
    host.addEventListener("scroll", () => {
      const slack = host.scrollHeight - host.scrollTop - host.clientHeight;
      this.follow = slack < 24;
    });
  }

  syncRows() {
    while (this.rows.length > this.emu.rows) this.rows.pop().remove();
    while (this.rows.length < this.emu.rows) {
      const div = document.createElement("div");
      div.className = "qt-line";
      this.viewEl.appendChild(div);
      this.rows.push(div);
    }
  }

  write(text) {
    this.emu.write(text);
    this.schedule();
  }

  resize(rows, cols) {
    this.emu.resize(rows, cols);
    this.syncRows();
    this.schedule();
  }

  clear() {
    this.emu.reset();
    this.scrollEl.innerHTML = "";
    this.rendered = 0;
    this.syncRows();
    this.schedule();
  }

  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => { this.frame = 0; this.paint(); });
  }

  paint() {
    const emu = this.emu;
    if (emu.clearedHistory) {
      // ESC[3J — the program asked for the history itself to go.
      this.scrollEl.innerHTML = "";
      this.rendered = 0;
      emu.clearedHistory = false;
    }
    if (emu.newScrollback.length) {
      const frag = document.createDocumentFragment();
      for (const line of emu.newScrollback) {
        const div = document.createElement("div");
        div.className = "qt-line";
        div.innerHTML = lineHtml(line) || "&nbsp;";
        frag.appendChild(div);
      }
      emu.newScrollback.length = 0;
      this.scrollEl.appendChild(frag);
    }
    // Lines the emulator dropped off the top of its ring have to leave the DOM
    // too, or the panel is a memory leak with a scrollbar.
    while (emu.trimmed > 0 && this.scrollEl.firstChild) {
      this.scrollEl.removeChild(this.scrollEl.firstChild);
      emu.trimmed--;
    }
    for (let r = 0; r < emu.rows; r++) {
      const line = emu.screen[r];
      if (!line) continue;
      if (!emu.allDirty && !line.dirty) continue;
      this.rows[r].innerHTML = lineHtml(line) || "&nbsp;";
      line.dirty = false;
    }
    this.hideBlankTail();
    emu.allDirty = false;
    if (this.follow) this.host.scrollTop = this.host.scrollHeight;
  }

  /** Drop the empty rows below the cursor, so the prompt is the last thing.
   *
   *  The emulator's screen is always exactly `rows` tall, which is correct —
   *  the pty was told that size and programs draw against it. But this panel
   *  scrolls the scrollback and the screen together, so rendering all of it
   *  put twenty blank lines under the prompt and "scroll to the bottom" landed
   *  on the last of them, leaving the output apparently stuck at the top with
   *  a wall of nothing beneath it.
   *
   *  Not applied on the alternate screen: `less` and `vim` own the full page,
   *  blank lines and all, and trimming theirs would move their status line.
   */
  hideBlankTail() {
    const emu = this.emu;
    if (emu.altScreen) {
      for (const row of this.rows) row.hidden = false;
      return;
    }
    let used = emu.row;
    for (let r = emu.rows - 1; r > used; r--) {
      if (emu.screen[r] && emu.screen[r].chars.length) { used = r; break; }
    }
    for (let r = 0; r < this.rows.length; r++) this.rows[r].hidden = r > used;
  }

  /** How many rows and columns fit, measured rather than assumed. */
  measure() {
    const probe = document.createElement("span");
    probe.className = "qt-probe";
    probe.textContent = "0".repeat(10);
    this.host.appendChild(probe);
    const rect = probe.getBoundingClientRect();
    probe.remove();
    const cw = rect.width / 10 || 8;
    const ch = rect.height || 17;
    const box = this.host.getBoundingClientRect();
    return {
      cols: Math.max(20, Math.floor((box.width - 16) / cw)),
      rows: Math.max(4, Math.floor((box.height - 8) / ch)),
    };
  }
}
