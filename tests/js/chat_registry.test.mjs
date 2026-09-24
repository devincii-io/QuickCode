import test from "node:test";
import assert from "node:assert/strict";
import { CardRegistry, MAIN } from "../../quickcode/frontend/js/chat/registry.js";

// A transcript as the chat builds it: blocks append, and a card appends inside
// its block — which, for a subagent, may sit above cards drawn after it.
function transcript() {
  const reg = new CardRegistry();
  const card = (scope, id, name, block) => reg.addCall({ scope, id, name, block, card: `${scope}:${id}` });
  return { reg, card };
}

test("a result finds its card by id, within the agent that made the call", () => {
  const { reg, card } = transcript();
  const step = reg.nextBlock();
  const agent = reg.nextBlock();
  const main = card(MAIN, "c1", "read", step);
  const sub = card("explore-1", "c1", "grep", agent);
  assert.equal(reg.call("c1"), main);
  assert.equal(reg.callIn("explore-1", "c1"), sub);
  assert.equal(reg.callIn("explore-2", "c1"), null);
  assert.equal(reg.call("missing"), null);
});

test("an id missing from its own agent falls back to the earliest card anywhere", () => {
  const { reg, card } = transcript();
  const agentA = reg.nextBlock();
  const agentB = reg.nextBlock();
  const later = card("b", "x", "read", agentB);
  const earlier = card("a", "x", "read", agentA);   // drawn after, but sits above
  assert.equal(reg.call("x", MAIN), earlier);
  assert.equal(reg.call("x", "b"), later);
});

test("a repeated id keeps resolving to the first card, as a document query did", () => {
  const { reg, card } = transcript();
  const block = reg.nextBlock();
  const first = card(MAIN, "dup", "read", block);
  card(MAIN, "dup", "read", block);
  assert.equal(reg.call("dup"), first);
});

test("running calls are tracked per agent and leave once settled", () => {
  const { reg, card } = transcript();
  const step = reg.nextBlock();
  const agent = reg.nextBlock();
  const a = card(MAIN, "a", "read", step);
  const b = card(MAIN, "b", "read", step);
  const s = card("sub", "a", "grep", agent);
  assert.deepEqual(reg.runningIn(MAIN), [a, b]);
  reg.settle(a);
  assert.deepEqual(reg.runningIn(MAIN), [b]);
  assert.deepEqual(reg.runningIn("sub"), [s]);
  assert.deepEqual(reg.runningIn("nobody"), []);
});

test("undecided cards of a tool come back in document order, not the order drawn", () => {
  const { reg, card } = transcript();
  const agent = reg.nextBlock();
  const step = reg.nextBlock();
  const mainCall = card(MAIN, "m", "bash", step);
  const subCall = card("sub", "s", "bash", agent);  // drawn later, inside the earlier block
  card(MAIN, "r", "read", step);
  assert.deepEqual(reg.undecidedFor("bash"), [subCall, mainCall]);
  reg.decide(subCall);
  assert.deepEqual(reg.undecidedFor("bash"), [mainCall]);
  assert.deepEqual(reg.undecidedFor("write"), []);
});

test("clearing forgets every card and agent", () => {
  const { reg, card } = transcript();
  card(MAIN, "a", "read", reg.nextBlock());
  reg.agents.set("sub", {});
  reg.clear();
  assert.equal(reg.call("a"), null);
  assert.deepEqual(reg.runningIn(MAIN), []);
  assert.deepEqual(reg.undecidedFor("read"), []);
  assert.equal(reg.agents.size, 0);
});
