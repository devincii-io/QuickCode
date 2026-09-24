import test from "node:test";
import assert from "node:assert/strict";
import {
  Attention, badgeTitle, noticeCopy, notifySupport, turnWatcher,
} from "../../quickcode/frontend/js/notify.js";

const state = (busy) => ["state", { type: "state", busy }];
const status = (s) => ["status", { type: "status", state: s }];
const event = (type, extra = {}) => ["event", { type, ...extra }];

function run(watch, steps, replaying = false) {
  return steps.map(([kind, ev]) => watch(kind, ev, replaying)).filter(Boolean);
}

test("a turn that ends is announced once, when busy clears", () => {
  const watch = turnWatcher();
  assert.deepEqual(run(watch, [state(false), state(true), status("streaming"), state(true)]), []);
  assert.deepEqual(run(watch, [status("idle"), state(false), state(false)]), [{ kind: "done" }]);
});

test("a failed turn is announced as an error, not as finished", () => {
  const watch = turnWatcher();
  assert.deepEqual(run(watch, [state(true), status("error"), event("error"), state(false)]),
    [{ kind: "error" }]);
  // The next turn starts clean.
  assert.deepEqual(run(watch, [state(true), state(false)]), [{ kind: "done" }]);
  // An error outside any turn is its own notice.
  assert.deepEqual(run(watch, [event("error", { message: "unknown mode" })]), [{ kind: "error" }]);
});

test("a turn you interrupted is not announced", () => {
  const watch = turnWatcher();
  assert.deepEqual(run(watch, [state(true), status("interrupted"), state(false)]), []);
  assert.deepEqual(run(watch, [state(true), state(false)]), [{ kind: "done" }]);
});

test("reviews are announced live, never from a replay", () => {
  const watch = turnWatcher();
  assert.deepEqual(run(watch, [event("permission_request", { tool: "bash" }), event("plan_request")], true), []);
  assert.deepEqual(run(watch, [event("permission_request", { tool: "bash" }), event("plan_request")]),
    [{ kind: "review", detail: "bash" }, { kind: "review", detail: "plan" }]);
});

test("a reset forgets a turn in flight: a new conversation is not its end", () => {
  const watch = turnWatcher();
  run(watch, [state(true)]);
  assert.deepEqual(run(watch, [["reset"], state(false)]), []);
});

test("attention counts per pane, keeps the most urgent kind, and sums a workspace", () => {
  const a = new Attention();
  assert.equal(a.add("p1", "done"), true);
  assert.equal(a.add("p1", "review"), true);
  a.add("p1", "done");
  a.add("p2", "error");
  assert.equal(a.add("p2", "bogus"), false);
  assert.deepEqual(a.get("p1"), { count: 3, kind: "review" });
  assert.deepEqual(a.sum(["p1", "p2", "missing"]), { count: 4, kind: "review" });
  assert.equal(a.sum(["missing"]), null);
  assert.equal(a.total(), 4);
  assert.equal(a.clear("p1"), true);
  assert.equal(a.clear("p1"), false);
  a.retain(new Set());
  assert.equal(a.total(), 0);
});

test("the title badge is added, replaced and removed without stacking", () => {
  assert.equal(badgeTitle("QuickCode", 2), "(2) QuickCode");
  assert.equal(badgeTitle("(2) QuickCode", 3), "(3) QuickCode");
  assert.equal(badgeTitle("(3) QuickCode", 0), "QuickCode");
  assert.equal(badgeTitle("Site | QuickCode", 150), "(99+) Site | QuickCode");
  assert.equal(badgeTitle("(99+) Site", 1), "(1) Site");
});

test("notification text names the agent and never the conversation", () => {
  assert.deepEqual(noticeCopy("review", "Fix login", "bash"),
    { title: "Fix login needs approval", body: "Waiting for permission to use bash." });
  assert.equal(noticeCopy("review", "", "plan").body, "A plan is waiting for your review.");
  assert.equal(noticeCopy("error", "X").title, "X stopped with an error");
  assert.equal(noticeCopy("done", "X").title, "X finished");
});

test("no Notification API reads as unsupported", () => {
  assert.equal(notifySupport(), "unsupported");
});
