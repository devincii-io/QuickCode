// The transcript's side of file checkpoints: a "Rewind files" button on each
// user turn that changed files, and a note where a rewind happened.
//
// A turn's block is found by its number — the `turn` every logged event
// carries — from a map filled as user messages are drawn, the way
// chat/registry.js finds a tool card by its call id: nothing here queries the
// transcript. What the rewind does is the dialog's (checkpoints/dialog.js).

import { openRewindDialog } from "../checkpoints/dialog.js";
import { TurnFiles, doneText, reasonText, rewoundLine } from "../checkpoints/model.js";

export class RewindMarks {
  /** `open(turn, {opener})` shows the dialog; injectable for tests. */
  constructor({ open = openRewindDialog } = {}) {
    this.open = open;
    this.clear();
  }

  clear() {
    this.files = new TurnFiles();
    this.turns = new Map();   // turn -> {node, button}
  }

  /** A user message's block was drawn. */
  user(ev, node) {
    const turn = Number(ev?.turn);
    if (!Number.isInteger(turn) || turn < 1 || !node) return;
    this.turns.set(turn, { node, button: null });
    this.sync(turn);
  }

  /** Any logged event. True when it was a checkpoint record the transcript
   *  has nothing more to do with; a `files_rewound` note goes out through
   *  `append`. A subagent's checkpoint is recorded and left to its card. */
  observe(ev, append) {
    if (ev?.type === "checkpoint") {
      this.sync(this.files.checkpoint(ev));
      return true;
    }
    if (ev?.type === "agent_event" && ev.ev?.type === "checkpoint") {
      this.sync(this.files.checkpoint(ev));
      return false;
    }
    if (ev?.type === "files_rewound") {
      for (const turn of this.files.rewound(ev)) this.sync(turn);
      append(rewoundNote(ev));
      return true;
    }
    return false;
  }

  sync(turn) {
    const mark = this.turns.get(turn);
    if (!mark) return;
    const count = this.files.count(turn);
    if (!count) {
      mark.button?.remove();
      mark.button = null;
      mark.node.classList.remove("has-rewind");
      return;
    }
    if (!mark.button) {
      mark.button = rewindButton(turn, (button) => this.open(turn, { opener: button }));
      mark.node.classList.add("has-rewind");
      mark.node.append(mark.button);
    }
    mark.button.title = `${count} ${count === 1 ? "file" : "files"} changed in this turn. `
      + "Rewind puts the files changed from this turn on back the way they were before it; "
      + "you see what would change first.";
  }
}

function rewindButton(turn, onOpen) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "turn-rewind";
  button.setAttribute("aria-label", `Rewind files to before turn ${turn}`);
  const glyph = document.createElement("span");
  glyph.setAttribute("aria-hidden", "true");
  glyph.textContent = "↺ ";
  button.append(glyph, "Rewind files");
  button.addEventListener("click", () => onOpen(button));
  return button;
}

function rewoundNote(ev) {
  const node = document.createElement("div");
  node.className = "sys-note rewind-note";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `↺ ${rewoundLine(ev)}`;
  const list = document.createElement("ul");
  for (const f of ev.files || []) list.append(item(f.path, doneText(f.action)));
  for (const s of ev.skipped || []) list.append(item(s.path, `left alone: ${reasonText(s.reason)}`));
  details.append(summary, list);
  node.append(details);
  return node;
}

function item(path, what) {
  const li = document.createElement("li");
  const code = document.createElement("code");
  code.textContent = path;
  li.append(code, ` — ${what}`);
  return li;
}
