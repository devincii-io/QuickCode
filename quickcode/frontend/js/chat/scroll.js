// Following the newest line of the transcript, at most once a frame.
//
// Whether to follow used to be measured after every event: a read of
// scrollHeight straight after a DOM write, so each event paid a synchronous
// layout of the whole transcript. Measuring at event time was also wrong under
// load: mid-way through the smooth scroll to the last event the reader is,
// briefly, far from the bottom, so the next event decided they had scrolled
// away and following stopped for good.
//
// Now the decision is the reader's, made where they make it — in the scroll
// handler — and the snap to the bottom is queued for the next frame, however
// many events land before it.

// Within this many pixels of the end the reader is "at the bottom": close
// enough that a line of new output should carry them along.
export const NEAR_BOTTOM_PX = 160;

/** Whether the reader still follows, given where a scroll event left them.
 *  Scrolling *up* away from the end lets go; anything that lands near the end
 *  takes hold again. A scroll that moved down but stopped short — the
 *  transcript's own smooth scroll, part-way to its target — changes nothing. */
export function following(was, { top, prevTop, height, client }) {
  if (height - top - client < NEAR_BOTTOM_PX) return true;
  if (top < prevTop) return false;
  return was;
}

export class Follower {
  /** `root` is the scroller; `beforeSnap` runs in the frame, before the jump. */
  constructor(root, { raf = (fn) => requestAnimationFrame(fn),
    caf = (id) => cancelAnimationFrame(id), beforeSnap = () => {} } = {}) {
    this.root = root;
    this.raf = raf;
    this.caf = caf;
    this.beforeSnap = beforeSnap;
    this.frame = 0;
    this.force = false;
    this.reset();
  }

  /** A fresh transcript: nothing to have scrolled away from. */
  reset() {
    if (this.frame) this.caf(this.frame);
    this.frame = 0;
    this.force = false;
    this.pinned = true;
    this.lastTop = this.root.scrollTop || 0;
  }

  /** The scroll listener. */
  onScroll() {
    const r = this.root;
    const top = r.scrollTop;
    this.pinned = following(this.pinned,
      { top, prevTop: this.lastTop, height: r.scrollHeight, client: r.clientHeight });
    this.lastTop = top;
  }

  /** Follow to the bottom on the next frame if the reader is there, or
   *  regardless (`force`, which also takes hold again). */
  request(force = false) {
    if (force) this.force = true;
    if (!this.frame) this.frame = this.raf(() => this.run());
  }

  /** Snap now, from inside a frame that is being painted anyway. */
  flush() {
    if (this.frame) this.caf(this.frame);
    this.run();
  }

  run() {
    this.frame = 0;
    const force = this.force;
    this.force = false;
    if (!force && !this.pinned) return;
    this.beforeSnap();
    if (force) this.pinned = true;
    this.root.scrollTop = this.root.scrollHeight;
  }
}
