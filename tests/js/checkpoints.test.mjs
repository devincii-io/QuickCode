import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_SELECTED, RewindSelection, TurnFiles, busyReason, countByAction, diffLines, fmtBytes,
  listingRows, refusal, resultSummary, rewoundLine,
} from "../../quickcode/frontend/js/checkpoints/model.js";
import { RewindMarks } from "../../quickcode/frontend/js/chat/rewind.js";

// ---- which turns can be rewound ----------------------------------------------

const cp = (turn, path, extra = {}) => ({ type: "checkpoint", seq: 1, turn, path, ...extra });

test("a checkpoint marks its turn, a subagent's included, and nothing else does", () => {
  const files = new TurnFiles();
  assert.equal(files.checkpoint(cp(2, "a.py")), 2);
  assert.equal(files.checkpoint(cp(2, "a.py")), 2, "the same file twice is one file");
  const sub = { type: "agent_event", agent_id: "general-1", turn: 3,
    ev: { type: "checkpoint", turn: 3, path: "b.py" } };
  assert.equal(files.checkpoint(sub), 3);
  assert.equal(files.checkpoint({ type: "tool_call", turn: 4 }), 0);
  assert.equal(files.checkpoint(cp(0, "c.py")), 0, "turn 0 is a legacy log, not a turn");
  assert.equal(files.checkpoint({ type: "checkpoint", turn: 5 }), 0, "no path, no mark");
  assert.deepEqual([files.count(2), files.count(3), files.count(4)], [1, 1, 0]);
});

test("a rewind takes its files off every turn from the one it went back to", () => {
  const files = new TurnFiles();
  files.checkpoint(cp(1, "a.py"));
  files.checkpoint(cp(2, "a.py"));
  files.checkpoint(cp(2, "b.py"));
  files.checkpoint(cp(3, "a.py"));
  const changed = files.rewound({ type: "files_rewound", to_turn: 2,
    files: [{ path: "a.py", action: "restored", from_turn: 2 }] });
  assert.deepEqual(changed.sort(), [2, 3]);
  assert.equal(files.count(1), 1, "a turn before the rewind keeps its mark");
  assert.equal(files.count(2), 1, "b.py was not rewound");
  assert.equal(files.count(3), 0);
  assert.deepEqual(files.rewound({ type: "files_rewound", to_turn: 1, files: [] }), []);
});

// ---- the selection ------------------------------------------------------------

function preview(rows) {
  return {
    turn: 3,
    untracked: "Files changed by bash … are not tracked",
    files: rows.map((r) => ({ action: "restore", restorable: true, blocked: "", conflicts: [],
      diff: "", added: 1, removed: 1, ...r })),
  };
}

const modified = [{ kind: "modified", detail: "changed on disk since turn 5's recorded edit" }];

test("a conflicted file starts unselected, and one that cannot be put back cannot be picked", () => {
  const sel = new RewindSelection(preview([
    { path: "a.py" },
    { path: "b.py", conflicts: modified },
    { path: "big.bin", restorable: false, reason: "too_large" },
    { path: "link.py", blocked: "the path now leads outside the project or through a link" },
  ]));
  assert.deepEqual(sel.selected.map((r) => r.path), ["a.py"]);
  assert.deepEqual(sel.selectable.map((r) => r.path), ["a.py", "b.py"]);
  assert.deepEqual(sel.conflicted.map((r) => r.path), ["b.py"]);
  sel.toggle("big.bin", true);
  assert.equal(sel.row("big.bin").selected, false);
  assert.deepEqual(sel.request(), { turn: 3, paths: ["a.py"] });
  assert.deepEqual(sel.verdict(), { ok: true, why: "" });
});

test("picking a conflicted file needs Overwrite anyway, which is the API's force", () => {
  const sel = new RewindSelection(preview([{ path: "a.py" }, { path: "b.py", conflicts: modified }]));
  sel.toggle("b.py", true);
  const blocked = sel.verdict();
  assert.equal(blocked.ok, false);
  assert.match(blocked.why, /1 selected file changed since its checkpoint/);
  sel.toggle("b.py", false);
  sel.setForce(true);
  assert.deepEqual(sel.selected.map((r) => r.path), ["a.py", "b.py"], "force takes them in");
  assert.deepEqual(sel.request(), { turn: 3, paths: ["a.py", "b.py"], force: true });
  sel.toggle("b.py", false);
  assert.deepEqual(sel.request(), { turn: 3, paths: ["a.py"] },
    "force is only sent for a conflicted file actually being rewound");
  sel.setForce(false);
  assert.deepEqual(sel.selected.map((r) => r.path), ["a.py"]);
});

test("select-all leaves conflicted files alone until they may be overwritten", () => {
  const sel = new RewindSelection(preview([{ path: "a.py" }, { path: "b.py", conflicts: modified }]));
  sel.setAll(false);
  assert.equal(sel.allState(), "none");
  assert.equal(sel.verdict().why, "Select at least one file.");
  sel.setAll(true);
  assert.deepEqual(sel.selected.map((r) => r.path), ["a.py"]);
  assert.equal(sel.allState(), "all");
  sel.setForce(true);
  sel.toggle("a.py", false);
  assert.equal(sel.allState(), "some");
});

test("a busy conversation, or an empty preview, cannot be rewound", () => {
  const sel = new RewindSelection(preview([{ path: "a.py" }]));
  const busy = busyReason({ busy: true });
  assert.match(busy, /a turn is running/);
  assert.deepEqual(sel.verdict({ busy }), { ok: false, why: busy });
  assert.match(busyReason({ busy: false, pending: [{ req_id: "r1" }] }), /permission prompt/);
  assert.equal(busyReason({ busy: false, pending: [] }), "");
  assert.equal(busyReason(null), "");
  const empty = new RewindSelection({ turn: 2, files: [] });
  assert.equal(empty.verdict().ok, false);
});

test("the paths are named unless every file is wanted and there are more than the API takes", () => {
  const many = Array.from({ length: MAX_SELECTED + 2 }, (_, i) => ({ path: `f${i}.txt` }));
  const sel = new RewindSelection(preview(many));
  assert.deepEqual(sel.request(), { turn: 3 });
  sel.toggle("f0.txt", false);
  assert.equal(sel.verdict().ok, false, "a subset over the limit cannot be named");
  sel.toggle("f1.txt", false);
  assert.equal(sel.verdict().ok, true);
  assert.equal(sel.request().paths.length, MAX_SELECTED);
  const few = new RewindSelection(preview([{ path: "a.py" }, { path: "b.py" }]));
  assert.deepEqual(few.request().paths, ["a.py", "b.py"],
    "every file, named: the rewind does what was previewed and nothing a later turn added");
});

test("a 409 after the preview marks the files that changed and leaves them out", () => {
  const sel = new RewindSelection(preview([{ path: "a.py" }, { path: "b.py" }]));
  sel.setForce(true);
  const marked = sel.conflictsArrived([{ path: "b.py", conflicts: modified }, { path: "gone.py" }]);
  assert.equal(marked, 1);
  assert.equal(sel.force, false, "a new conflict is a new question");
  assert.deepEqual(sel.selected.map((r) => r.path), ["a.py"]);
  assert.deepEqual(sel.row("b.py").conflicts, modified);
});

// ---- what the server said ---------------------------------------------------

test("a refusal reads as the API wrote it, structured or not", () => {
  const conflict = Object.assign(new Error("409: files changed"), {
    status: 409,
    detail: { message: "files changed since their checkpoints; …", conflicts: [{ path: "a.py" }] },
  });
  assert.deepEqual(refusal(conflict), {
    message: "files changed since their checkpoints; …", conflicts: [{ path: "a.py" }], paths: [],
  });
  const busy = Object.assign(new Error("409: x"), {
    status: 409, detail: "conversation is busy (a background subagent job is running); rewind once it is idle",
  });
  assert.equal(refusal(busy).message,
    "conversation is busy (a background subagent job is running); rewind once it is idle");
  assert.equal(refusal(new Error("500: Internal Server Error")).message, "Internal Server Error");
  assert.deepEqual(refusal({ detail: { message: "no checkpoint", paths: ["x"] } }).paths, ["x"]);
});

test("a result and a logged rewind say what happened to each file", () => {
  const result = {
    turn: 3, rewind_id: "rw2",
    files: [
      { path: "a.py", action: "restored", from_turn: 3, backup: true },
      { path: "n.md", action: "deleted", from_turn: 3, backup: false },
      { path: "c.py", action: "created", from_turn: 4, backup: false },
    ],
    skipped: [{ path: "big.bin", reason: "too_large" }],
  };
  const s = resultSummary(result);
  assert.equal(s.headline, "Rewound to before turn 3: 1 restored, 1 deleted, 1 re-created.");
  assert.deepEqual(s.files.map((f) => f.note), ["", "what it replaced was not kept", ""]);
  assert.deepEqual(s.skipped, [{ path: "big.bin", reason: "too large to keep a copy of" }]);
  assert.equal(resultSummary({ turn: 1, rewind_id: null, files: [] }).headline,
    "Nothing was put back.");
  assert.equal(countByAction([{ action: "unchanged" }, { action: "restored" }]),
    "1 restored, 1 unchanged");
  assert.equal(rewoundLine({ to_turn: 2, files: [{ path: "a", action: "restored" }],
    file_count: 250, skipped: [{ path: "b", reason: "damaged" }], forced: true }),
  "files rewound to before turn 2 · 1 restored · 250 in all · 1 skipped · overwrote changes made since");
});

// ---- the checkpoint list ------------------------------------------------------

test("the panel lists the newest turn first, with what became of each file", () => {
  const rows = listingRows({
    checkpoints: [
      { turn: 1, time: "2026-09-24T10:00:00", restorable: false, files: [
        { path: "a.py", change: "modified", added: 2, removed: 1, restorable: false,
          rewound: "rw1", agent: "main" },
      ] },
      { turn: 3, time: "2026-09-24T10:05:00", restorable: true, files: [
        { path: "big.bin", change: "created", added: null, removed: null, binary: true,
          restorable: false, reason: "too_large", rewound: null, agent: "general-1" },
        { path: "b.py", change: "deleted", added: 0, removed: 4, restorable: true, agent: "main" },
      ] },
    ],
  });
  assert.deepEqual(rows.map((r) => [r.turn, r.rewindable]), [[3, true], [1, false]]);
  assert.deepEqual(rows[0].files, [
    { path: "big.bin", change: "created", counts: "binary",
      state: "not kept: too large to keep a copy of", agent: "general-1" },
    { path: "b.py", change: "deleted", counts: "+0 −4", state: "", agent: "" },
  ]);
  assert.equal(rows[1].files[0].state, "rewound (rw1)");
  assert.deepEqual(listingRows(null), []);
  assert.deepEqual([0, 1023, 18_233, 134_217_728].map(fmtBytes),
    ["0 B", "1023 B", "17.8 KB", "128.0 MB"]);
});

// ---- the diff ---------------------------------------------------------------

test("diff lines keep a removed '--' line a removal and drop the CR only for display", () => {
  const text = "--- a/x.txt\r\n+++ b/x.txt\n@@ -1,2 +1,2 @@\n--- not a header\r\n+back\n same\n"
    + "\\ No newline at end of file\n";
  assert.deepEqual(diffLines(text), [
    { kind: "file", text: "--- a/x.txt" },
    { kind: "file", text: "+++ b/x.txt" },
    { kind: "hunk", text: "@@ -1,2 +1,2 @@" },
    { kind: "del", text: "--- not a header" },
    { kind: "add", text: "+back" },
    { kind: "ctx", text: " same" },
    { kind: "note", text: "\\ No newline at end of file" },
  ], "the shared js/diff.js kinds, with no blank line after the last one");
  assert.deepEqual(diffLines(""), []);
});

// ---- the transcript's marks -------------------------------------------------

// Just enough DOM for chat/rewind.js: it builds a button and a note, and
// touches the user block only through the node it was handed.
function fakeDocument() {
  const make = (tag) => {
    const node = {
      tag, children: [], attrs: {}, listeners: {}, className: "", textContent: "", parent: null,
      classList: {
        set: new Set(),
        add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
        contains(c) { return this.set.has(c); },
      },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] ?? null; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      append(...kids) { for (const k of kids) { if (typeof k === "object") k.parent = this; this.children.push(k); } },
      remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; },
    };
    return node;
  };
  globalThis.document = { createElement: make };
  return make;
}

test("a user turn gets its Rewind button when a checkpoint names it, found by turn number", () => {
  const make = fakeDocument();
  const opened = [];
  const marks = new RewindMarks({ open: (turn, opts) => opened.push([turn, opts.opener]) });
  const turn1 = make("div"), turn2 = make("div");
  marks.user({ type: "user_message", turn: 1 }, turn1);
  marks.user({ type: "user_message", turn: 2 }, turn2);
  const appended = [];
  assert.equal(marks.observe(cp(2, "a.py"), (n) => appended.push(n)), true,
    "a checkpoint draws nothing of its own");
  assert.equal(turn1.children.length, 0);
  const [button] = turn2.children;
  assert.equal(button.getAttribute("aria-label"), "Rewind files to before turn 2");
  assert.equal(button.type, "button", "a real button: keyboard reachable");
  assert.ok(turn2.classList.contains("has-rewind"));
  button.listeners.click();
  assert.deepEqual(opened, [[2, button]]);

  const sub = { type: "agent_event", agent_id: "g-1", turn: 1, ev: { type: "checkpoint", turn: 1, path: "b.py" } };
  assert.equal(marks.observe(sub, () => {}), false, "a subagent's event still reaches its card");
  assert.equal(turn1.children.length, 1);

  assert.equal(marks.observe({ type: "files_rewound", to_turn: 1, file_count: 2,
    files: [{ path: "a.py", action: "restored" }, { path: "b.py", action: "restored" }],
    skipped: [] }, (n) => appended.push(n)), true);
  assert.equal(appended.length, 1, "the rewind leaves a note");
  assert.equal(turn1.children.length + turn2.children.length, 0, "nothing left to rewind");
  assert.ok(!turn2.classList.contains("has-rewind"));
  assert.equal(marks.observe({ type: "tool_call", id: "c1" }, () => {}), false);
  delete globalThis.document;
});
