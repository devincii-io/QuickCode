import test from "node:test";
import assert from "node:assert/strict";

const frames = new Map();
let nextId = 1;
globalThis.requestAnimationFrame = (fn) => { frames.set(nextId, fn); return nextId++; };
globalThis.cancelAnimationFrame = (id) => { frames.delete(id); };
const tick = () => {
  const due = [...frames.values()];
  frames.clear();
  due.forEach((fn) => fn());
};

const { perFrame } = await import("../../quickcode/frontend/js/frame.js");

test("any number of calls before a frame run the work once, at the frame", () => {
  let runs = 0;
  const soon = perFrame(() => runs++);
  soon(); soon(); soon();
  assert.equal(runs, 0);
  assert.equal(frames.size, 1);
  assert.ok(soon.pending());
  tick();
  assert.equal(runs, 1);
  assert.ok(!soon.pending());
  soon();
  tick();
  assert.equal(runs, 2, "a call after the frame queues the next one");
});

test("flush runs queued work now and not again at the frame; with nothing queued it does nothing", () => {
  let runs = 0;
  const soon = perFrame(() => runs++);
  soon.flush();
  assert.equal(runs, 0);
  soon();
  soon.flush();
  assert.equal(runs, 1);
  tick();
  assert.equal(runs, 1);
});

test("cancel drops queued work, and the next call queues afresh", () => {
  let runs = 0;
  const soon = perFrame(() => runs++);
  soon();
  soon.cancel();
  tick();
  assert.equal(runs, 0);
  soon();
  tick();
  assert.equal(runs, 1);
});

test("work that queues itself again runs on the following frame", () => {
  let runs = 0;
  const soon = perFrame(() => { runs++; if (runs < 3) soon(); });
  soon();
  tick(); tick(); tick();
  assert.equal(runs, 3);
});
