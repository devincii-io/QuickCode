// Only the newest stretch of a long transcript is in the document.
//
// Every top-level block (a message, a step of tool calls, a subagent card, a
// note) is built as it arrives and kept, but a 10k-event session is over a
// hundred thousand nodes, and a document that size made every layout, every
// copy-button sweep (copy.js) and every scroll snap cost tens of milliseconds.
// So a replay builds its blocks off-document and attaches only the newest
// KEEP; the rest wait, fully built and still patched by later events through
// the references chat.js holds, until the reader scrolls up to them. A reader
// who follows a long live session sheds the backlog above in whole chunks.

export const KEEP = 120;        // blocks attached after a replay, and kept while following
export const CHUNK = 80;        // blocks brought back per step when scrolling up
export const REVEAL_PX = 600;   // this close to the top, the previous chunk comes back

/** Where the attached range starts when only the newest `keep` of `count` stay. */
export function tailStart(count, keep = KEEP) { return Math.max(0, count - keep); }

/** Where it starts once the chunk before `first` is back. */
export function revealStart(first, chunk = CHUNK) { return Math.max(0, first - chunk); }

/** Where it starts after shedding a follower's backlog — only once a whole
 *  chunk more than `keep` has built up, so it is not a per-event cost. */
export function trimStart(first, count, keep = KEEP, chunk = CHUNK) {
  return count - first >= keep + chunk ? count - keep : first;
}

export class TranscriptWindow {
  constructor(root) {
    this.root = root;
    this.reset();
  }

  /** The transcript was emptied (the caller cleared the root). */
  reset() {
    this.blocks = [];        // every block, oldest first
    this.first = 0;          // blocks[first, end) are in the document
    this.end = 0;
    this.deferred = false;   // building off-document
    this.more = null;        // the "show earlier" button, while anything is held back
  }

  /** Build what follows off-document until `settle()`: a replay. */
  defer() { this.deferred = true; }

  append(node) {
    this.blocks.push(node);
    if (this.deferred) return;
    this.root.appendChild(node);
    this.end = this.blocks.length;
  }

  remove(node) {
    const i = this.blocks.lastIndexOf(node);
    if (i >= 0) {
      this.blocks.splice(i, 1);
      if (i < this.first) this.first -= 1;
      if (i < this.end) this.end -= 1;
    }
    node.remove();
    if (i >= 0 && i < this.first) this.syncMore();
  }

  /** Attach what was built off-document: the newest KEEP blocks, in one go. */
  settle() {
    if (!this.deferred) return;
    this.deferred = false;
    const count = this.blocks.length;
    const first = Math.max(this.first, tailStart(count));
    for (let i = this.first; i < Math.min(first, this.end); i++) this.blocks[i].remove();
    const batch = this.root.ownerDocument.createDocumentFragment();
    for (let i = Math.max(first, this.end); i < count; i++) batch.appendChild(this.blocks[i]);
    this.root.appendChild(batch);
    this.first = first;
    this.end = count;
    this.syncMore();
  }

  /** Put the previous chunk back above what is shown, keeping what the reader
   *  is looking at where it is. Returns whether there was anything to show. */
  revealOlder() {
    if (this.deferred || !this.first) return false;
    const start = revealStart(this.first);
    const anchor = this.blocks[this.first] || null;
    const before = anchor?.getBoundingClientRect().top;
    const batch = this.root.ownerDocument.createDocumentFragment();
    for (let i = start; i < this.first; i++) batch.appendChild(this.blocks[i]);
    this.root.insertBefore(batch, anchor);
    this.first = start;
    this.syncMore();
    // A browser with scroll anchoring has already held the anchor in place;
    // one without (WebKit) has not.
    if (anchor) {
      const moved = anchor.getBoundingClientRect().top - before;
      if (Math.abs(moved) >= 1) {
        this.root.scrollTo({ top: this.root.scrollTop + moved, behavior: "instant" });
      }
    }
    return true;
  }

  /** Shed the backlog above a reader who is following the newest line. */
  trim() {
    if (this.deferred) return;
    const first = trimStart(this.first, this.blocks.length);
    if (first === this.first) return;
    for (let i = this.first; i < first; i++) this.blocks[i].remove();
    this.first = first;
    this.syncMore();
  }

  /** Every block, attached or not, in order. */
  all() { return this.blocks.slice(); }

  syncMore() {
    if (!this.first) {
      this.more?.remove();
      this.more = null;
      return;
    }
    if (!this.more) {
      this.more = this.root.ownerDocument.createElement("button");
      this.more.type = "button";
      this.more.className = "chat-more";
      this.more.addEventListener("click", () => this.revealOlder());
    }
    const n = this.first;
    this.more.textContent = `show ${n.toLocaleString()} earlier ${n === 1 ? "item" : "items"}`;
    const anchor = this.blocks[this.first] || null;
    if (!this.more.isConnected || this.more.nextSibling !== anchor) {
      this.root.insertBefore(this.more, anchor);
    }
  }
}
