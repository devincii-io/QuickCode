// The assistant message still being written: one node, patched in place.
//
// Deltas arrive far faster than a screen refreshes, and each one used to
// re-parse and re-insert the whole reply: quadratic in its length, with every
// code block rebuilt (and re-decorated by copy.js) dozens of times a second.
// Now chat.js paints at most once a frame, and a paint appends the blocks
// markdown.js has finished and replaces just the open one.

import { markdownStream } from "../markdown.js";
import { el } from "../util.js";

function fragment(html) {
  const t = document.createElement("template");
  t.innerHTML = html;
  return t.content;
}

export class LiveBubble {
  /** `blocks` places and removes the node (chat/window.js). */
  constructor(blocks) {
    this.blocks = blocks;
    this.forget();
  }

  /** The transcript was emptied under it. */
  forget() {
    this.node = null;
    this.md = null;            // its incremental renderer (markdown.js)
    this.tail = [];            // its nodes the next update replaces
    this.reasoningText = null; // the text node inside its <details>
  }

  /** Draw what has streamed so far. `pending`: a tool call is streaming,
   *  which keeps the bubble even before it has text. */
  render({ text, reasoning, pending }) {
    if (!text && !reasoning && !pending) {
      // Emptied without the message that normally replaces it: a replayed
      // message superseded what streamed (store.js). What is on screen is stale.
      if (this.node) this.drop();
      return;
    }
    const node = this.ensure();
    if (reasoning) {
      // Built once and then only its text moves, so collapsing it mid-stream
      // sticks instead of being re-opened by the next delta.
      if (!this.reasoningText) {
        const details = el(`<details class="reasoning" open><summary>thinking</summary></details>`);
        this.reasoningText = details.appendChild(document.createTextNode(""));
        node.querySelector(".reasoning-slot").appendChild(details);
      }
      if (this.reasoningText.data !== reasoning) this.reasoningText.data = reasoning;
    }
    const bubble = node.querySelector(".bubble");
    const { reset, commit, tail } = this.md.update(text);
    if (reset) {
      for (const child of [...bubble.childNodes]) {
        if (!child.classList?.contains("copy-btn")) child.remove();
      }
    }
    for (const n of this.tail) n.remove();
    if (commit) bubble.appendChild(fragment(commit));
    const rest = fragment(tail);
    this.tail = [...rest.childNodes];
    bubble.appendChild(rest);
  }

  ensure() {
    if (!this.node) {
      this.node = el(`<div class="msg msg-assistant">
          <div class="reasoning-slot"></div><div class="bubble"></div></div>`);
      this.md = markdownStream();
      this.tail = [];
      this.reasoningText = null;
      this.blocks.append(this.node);
    }
    return this.node;
  }

  drop() {
    if (!this.node) return;
    this.blocks.remove(this.node);
    this.node = null;
  }

  // Drop the bubble if nothing has been drawn in it. It is created as soon
  // as *anything* streams — including a tool call's arguments — so a round that
  // went straight to a tool left an empty message behind, once per round, each
  // one a stray gap in the transcript. (Its text cannot be the test: copy.js puts
  // a "copy" button in every bubble.)
  //
  // A bubble that has text stays live. Its assistant_message always follows —
  // session/recorder.py flushes one before each tool call, at the end of every
  // round and on an interrupt — and replaces it. Letting it go here, when an
  // unrelated event such as a mode switch landed mid-reply, left the partial copy
  // on the page and streamed the whole reply a second time underneath it.
  closeIfBlank() {
    if (this.node && !this.node.querySelector(".reasoning, .bubble > :not(.copy-btn)")) {
      this.drop();
    }
  }
}
