import test from "node:test";
import assert from "node:assert/strict";

const writes = new Map();
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    let text = "";
    nodes.set(id, {
      className: "", title: "", innerHTML: "",
      classList: { add() {}, remove() {}, toggle() {} },
      get textContent() { return text; },
      set textContent(v) { text = v; writes.set(id, (writes.get(id) || 0) + 1); },
    });
  }
  return nodes.get(id);
}
globalThis.document = { getElementById: node, querySelector: () => null };

const frames = [];
globalThis.requestAnimationFrame = (fn) => frames.push(fn);
const flushFrames = () => { for (const fn of frames.splice(0)) fn(); };

const { ingest } = await import("../../quickcode/frontend/js/store.js");
const { initStatusBar } = await import("../../quickcode/frontend/js/statusbar.js");
initStatusBar();

const ledger = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
let seq = 0;
const logged = (type) => ingest({ type, seq: ++seq, id: `c${seq}`, ms: 1 });

test("a replay draws the metrics once, when it is done", () => {
  ingest({ type: "state", model: "m", mode: "ask", ledger, queued: 0 });
  writes.clear();
  ingest({ type: "replay_start" });
  for (let i = 0; i < 10_000; i++) logged(i % 10 ? "tool_call" : "user_message");
  assert.equal(frames.length, 0, "a replayed event scheduled a repaint");
  assert.equal(writes.get("st-work") ?? 0, 0);
  ingest({ type: "replay_done" });
  assert.equal(writes.get("st-work"), 1);
  assert.equal(node("st-work").textContent, "1000 turns · 9000 steps");
});

test("live events repaint the metrics at most once a frame", () => {
  writes.clear();
  for (let i = 0; i < 50; i++) logged("tool_call");
  assert.equal(frames.length, 1);
  assert.equal(writes.get("st-work") ?? 0, 0);
  flushFrames();
  assert.equal(writes.get("st-work"), 1);
  assert.equal(node("st-work").textContent, "1000 turns · 9050 steps");
  logged("user_message");
  flushFrames();
  assert.equal(node("st-work").textContent, "1001 turns · 9050 steps");
});
