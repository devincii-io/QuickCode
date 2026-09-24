import test from "node:test";
import assert from "node:assert/strict";
import { ingest, resetConversation, store, subscribe } from "../../quickcode/frontend/js/store.js";

// One socket's view of the wire, as ws.js feeds it: the state event, the
// replay between its markers, then whatever the live queue delivers.
function attach(state, replay, live = []) {
  resetConversation();
  ingest({ type: "state", busy: false, pending: [], ...state });
  ingest({ type: "replay_start" });
  for (const ev of replay) ingest(ev);
  ingest({ type: "replay_done" });
  for (const ev of live) ingest(ev);
}

test("deltas the live queue repeats after a replay do not survive their own duplicate", () => {
  const seen = [];
  const off = subscribe((kind) => seen.push(kind));
  const message = { type: "assistant_message", seq: 2, turn: 1, text: "hello", finish_reason: "stop" };
  attach({ busy: true }, [
    { type: "user_message", seq: 1, turn: 1, text: "hi" },
    message,
  ], [
    // Queued between the attach and the snapshot: already part of `message`.
    { type: "text_delta", text: "hel" },
    { type: "reasoning_delta", text: "hm" },
    { type: "text_delta", text: "lo" },
  ]);
  assert.equal(store.streamText, "hello");
  seen.length = 0;
  ingest({ ...message });
  off();
  assert.equal(store.streamText, "");
  assert.equal(store.streamReasoning, "");
  assert.deepEqual(seen, ["stream"], "the chat has to hear that its live bubble is stale");
  assert.equal(store.events.filter((e) => e.seq === 2).length, 1);
});

test("a repeated tool call closes the pending call it assembled", () => {
  const call = { type: "tool_call", seq: 2, turn: 1, id: "c1", name: "bash", arguments: "{}" };
  attach({ busy: true }, [{ type: "user_message", seq: 1, turn: 1, text: "go" }, call], [
    { type: "tool_call_start", id: "c1", name: "bash" },
  ]);
  assert.equal(store.pendingCalls.size, 1);
  ingest({ ...call });
  assert.equal(store.pendingCalls.size, 0);
});

test("a duplicate that supersedes nothing stays silent", () => {
  const seen = [];
  const message = { type: "assistant_message", seq: 1, turn: 1, text: "x", finish_reason: "stop" };
  attach({}, [message]);
  const off = subscribe((kind) => seen.push(kind));
  ingest({ ...message });
  off();
  assert.deepEqual(seen, []);
});

test("attaching mid-turn resumes a running status instead of reading idle", () => {
  const statuses = [];
  const off = subscribe((kind, ev) => { if (kind === "status") statuses.push(ev.state); });
  attach({ busy: true }, [{ type: "user_message", seq: 1, turn: 1, text: "go" }]);
  assert.equal(store.agentStatus, "streaming");
  attach({ busy: true }, [
    { type: "user_message", seq: 1, turn: 1, text: "go" },
    { type: "tool_call", seq: 2, turn: 1, id: "c1", name: "bash", arguments: "{}" },
  ]);
  assert.equal(store.agentStatus, "executing_tools");
  attach({ busy: false }, [{ type: "user_message", seq: 1, turn: 1, text: "go" }]);
  off();
  assert.equal(store.agentStatus, "idle");
  assert.deepEqual(statuses, ["streaming", "executing_tools"]);
});

test("calls an earlier turn never finished are not in flight after a replay", () => {
  const interrupted = { type: "tool_call", seq: 2, turn: 1, id: "old", name: "grep", arguments: "{}" };
  attach({ busy: false }, [{ type: "user_message", seq: 1, turn: 1, text: "a" }, interrupted]);
  assert.equal(store.runningTools.size, 0);
  attach({ busy: true }, [
    { type: "user_message", seq: 1, turn: 1, text: "a" },
    interrupted,
    { type: "user_message", seq: 3, turn: 2, text: "b" },
    { type: "tool_call", seq: 4, turn: 2, id: "new", name: "bash", arguments: "{}" },
  ]);
  assert.deepEqual([...store.runningTools.keys()], ["new"]);
});
