import test from "node:test";
import assert from "node:assert/strict";
import { ingest, resetConversation, store } from "../../quickcode/frontend/js/store.js";

function replay(busy, events) {
  resetConversation();
  ingest({ type: "state", busy });
  ingest({ type: "replay_start" });
  for (const ev of events) ingest(ev);
  ingest({ type: "replay_done" });
}

const unanswered = [
  { type: "user_message", seq: 1, text: "go" },
  { type: "tool_call", seq: 2, id: "c1", name: "bash", arguments: "{}" },
];

test("a replayed call the log never answered is not left running once the server says idle", () => {
  // An interrupt answers the call in the message history, not with a
  // tool_result, and replay carries no status events: without this the call
  // stayed "running" until the end of the next turn, so the trajectory drew it
  // growing to "now" for ever and the activity line named it again.
  replay(false, unanswered);
  assert.equal(store.runningTools.size, 0);
  assert.equal(store.pendingCalls.size, 0);
});

test("a replayed call stays running while the server says the turn is still going", () => {
  replay(true, unanswered);
  assert.deepEqual([...store.runningTools.keys()], ["c1"]);
});
