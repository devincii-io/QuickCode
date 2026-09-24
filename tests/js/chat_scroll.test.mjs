import test from "node:test";
import assert from "node:assert/strict";
import { Follower, NEAR_BOTTOM_PX, following } from "../../quickcode/frontend/js/chat/scroll.js";

// A scroller with a 500px viewport whose content height the test sets, and a
// frame queue the test runs by hand.
function scroller(height = 2000) {
  const root = { scrollTop: 0, scrollHeight: height, clientHeight: 500 };
  const frames = [];
  const follower = new Follower(root, {
    raf: (fn) => frames.push(fn),
    caf: () => { frames.length = 0; },
  });
  const tick = () => { const run = frames.splice(0); run.forEach((fn) => fn()); return run.length; };
  const scrollTo = (top) => { root.scrollTop = top; follower.onScroll(); };
  return { root, follower, frames, tick, scrollTo };
}

test("near the end holds on; scrolling up and away lets go", () => {
  const at = (top, prevTop) => ({ top, prevTop, height: 2000, client: 500 });
  assert.equal(following(false, at(1500 - NEAR_BOTTOM_PX + 1, 0)), true);
  assert.equal(following(true, at(900, 1500)), false);
  // Moving down but short of the end — the transcript's own smooth scroll on
  // its way to the bottom — changes nothing either way.
  assert.equal(following(true, at(900, 400)), true);
  assert.equal(following(false, at(900, 400)), false);
});

test("any number of events in one frame snap to the bottom once", () => {
  const { root, follower, frames, tick } = scroller();
  for (let i = 0; i < 50; i++) follower.request();
  assert.equal(frames.length, 1);
  root.scrollHeight = 2600;
  assert.equal(tick(), 1);
  assert.equal(root.scrollTop, 2600);
  assert.equal(tick(), 0);
});

test("a reader who scrolled up is not pulled back down by new events", () => {
  const { root, follower, tick, scrollTo } = scroller();
  scrollTo(1500);                    // at the end
  scrollTo(600);                     // and back up to read
  root.scrollHeight = 3000;
  follower.request();
  tick();
  assert.equal(root.scrollTop, 600);
  scrollTo(2400);                    // back within reach of the end
  root.scrollHeight = 3200;
  follower.request();
  tick();
  assert.equal(root.scrollTop, 3200);
});

test("a big block landing while following does not break the follow", () => {
  // Measured after the append, a 1000px card put the reader 1000px from the
  // end and following stopped. The reader has not moved, so it must not.
  const { root, follower, tick, scrollTo } = scroller();
  scrollTo(1500);
  root.scrollHeight = 3000;
  follower.request();
  tick();
  assert.equal(root.scrollTop, 3000);
});

test("a forced snap follows even a reader who scrolled away, and takes hold again", () => {
  const { root, follower, tick, scrollTo } = scroller();
  scrollTo(1500);
  scrollTo(100);
  follower.request(true);
  tick();
  assert.equal(root.scrollTop, 2000);
  assert.equal(follower.pinned, true);
});

test("flushing inside a frame snaps now and drops the queued frame", () => {
  const { root, follower, frames } = scroller();
  follower.request();
  root.scrollHeight = 2400;
  follower.flush();
  assert.equal(root.scrollTop, 2400);
  assert.equal(frames.length, 0);
});

test("reset drops a queued snap and follows from the start", () => {
  const { follower, frames, scrollTo } = scroller();
  scrollTo(1500);
  scrollTo(100);
  follower.request();
  follower.reset();
  assert.equal(frames.length, 0);
  assert.equal(follower.pinned, true);
});
