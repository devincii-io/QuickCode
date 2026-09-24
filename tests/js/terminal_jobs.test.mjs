// The Jobs tab's bookkeeping (js/terminal/jobs_state.js): how list answers and
// socket events combine into one set of rows, and how output reads combine
// into one bounded text. The view is DOM; these are the parts that decide
// what it shows.
import test from "node:test";
import assert from "node:assert/strict";
import {
  applyJobEvent, jobChip, mergeJobs, mergeTail, orderJobs, pickJob, runningCount, upsertJob,
  fmtBytes, fmtDuration,
} from "../../quickcode/frontend/js/terminal/jobs_state.js";

const row = (id, over = {}) => ({
  id, command: `echo ${id}`, description: "", label: "", status: "running",
  exit_code: null, killed_by: "", started: 100, ended: null, seconds: 1, bytes: 0, dropped: 0,
  ...over,
});
const answer = (over) => ({ text: "", start: 0, next: 0, end: 0, gap: 0, ...over });

// ---- rows: list answers and events ----

test("a list answer read before a job ended cannot bring it back to life", () => {
  let jobs = mergeJobs(new Map(), [row("bash_1")]);
  jobs = applyJobEvent(jobs, { type: "bash_job_done", job_id: "bash_1", status: "exited", exit_code: 2, seconds: 3 });
  jobs = mergeJobs(jobs, [row("bash_1", { bytes: 40 })]);
  const job = jobs.get("bash_1");
  assert.equal(job.status, "exited");
  assert.equal(job.exit_code, 2);
  assert.equal(job.bytes, 40);
});

test("a job only an event has seen survives a list read a moment before it started", () => {
  let jobs = mergeJobs(new Map(), [row("bash_1")]);
  jobs = applyJobEvent(jobs, { type: "bash_job_started", job_id: "bash_2", command: "npm run dev" }, 200);
  jobs = mergeJobs(jobs, [row("bash_1")]);
  assert.deepEqual([...jobs.keys()], ["bash_1", "bash_2"]);
  assert.equal(jobs.get("bash_2").label, "npm run dev");
  assert.equal(jobs.get("bash_2").started, 200);
  // Once a list has named it, it is an ordinary row: absent from the next
  // list means the table let it go.
  jobs = mergeJobs(jobs, [row("bash_1"), row("bash_2")]);
  jobs = mergeJobs(jobs, [row("bash_1")]);
  assert.deepEqual([...jobs.keys()], ["bash_1"]);
});

test("output notes only ever raise the byte count, and name only known jobs", () => {
  let jobs = mergeJobs(new Map(), [row("bash_1", { bytes: 100 })]);
  const same = applyJobEvent(jobs, { type: "bash_job_output", job_id: "bash_1", bytes: 50 });
  assert.equal(same, jobs);
  assert.equal(applyJobEvent(jobs, { type: "bash_job_output", job_id: "bash_9", bytes: 5 }), jobs);
  jobs = applyJobEvent(jobs, { type: "bash_job_output", job_id: "bash_1", bytes: 150 });
  assert.equal(jobs.get("bash_1").bytes, 150);
  // A started event for a row already known changes nothing.
  assert.equal(applyJobEvent(jobs, { type: "bash_job_started", job_id: "bash_1", command: "x" }), jobs);
  assert.equal(applyJobEvent(jobs, { type: "tool_call", id: "c1" }), jobs);
});

test("a user's kill reads as one, and exit codes as ok or not", () => {
  let jobs = mergeJobs(new Map(), [row("bash_1"), row("bash_2"), row("bash_3")]);
  jobs = applyJobEvent(jobs, { type: "bash_job_done", job_id: "bash_1", status: "killed", killed_by: "user" });
  jobs = applyJobEvent(jobs, { type: "bash_job_done", job_id: "bash_2", status: "killed" });
  jobs = applyJobEvent(jobs, { type: "bash_job_done", job_id: "bash_3", status: "exited", exit_code: 0 });
  assert.deepEqual(jobChip(jobs.get("bash_1")), { kind: "killed", text: "killed by you" });
  assert.deepEqual(jobChip(jobs.get("bash_2")), { kind: "killed", text: "killed" });
  assert.deepEqual(jobChip(jobs.get("bash_3")), { kind: "ok", text: "exit 0" });
  assert.deepEqual(jobChip(row("bash_4", { status: "exited", exit_code: 1 })), { kind: "error", text: "exit 1" });
  assert.deepEqual(jobChip(row("bash_5")), { kind: "running", text: "running" });
  assert.equal(runningCount(jobs), 0);
});

test("a kill's answer is settled like a list row", () => {
  let jobs = mergeJobs(new Map(), [row("bash_1", { bytes: 90 })]);
  jobs = upsertJob(jobs, row("bash_1", { status: "killed", killed_by: "user", bytes: 10 }));
  assert.equal(jobs.get("bash_1").status, "killed");
  assert.equal(jobs.get("bash_1").bytes, 90);
});

test("newest first by number, and the selection sticks while it exists", () => {
  const jobs = mergeJobs(new Map(), [
    row("bash_9", { status: "exited", exit_code: 0 }), row("bash_10"), row("bash_2"),
  ]);
  assert.deepEqual(orderJobs(jobs).map((j) => j.id), ["bash_10", "bash_9", "bash_2"]);
  assert.equal(pickJob(jobs, "bash_2"), "bash_2");
  assert.equal(pickJob(jobs, "bash_77"), "bash_10");
  const done = mergeJobs(new Map(), [row("bash_1", { status: "killed" }), row("bash_3", { status: "exited" })]);
  assert.equal(pickJob(done, null), "bash_3");
  assert.equal(pickJob(new Map(), "bash_1"), null);
});

// ---- output: tail merge ----

test("reads that continue each other append", () => {
  let buf = mergeTail(null, answer({ text: "one\n", next: 4, end: 4 }));
  buf = mergeTail(buf, answer({ text: "two\n", start: 4, next: 8, end: 8 }));
  assert.equal(buf.text, "one\ntwo\n");
  assert.equal(buf.next, 8);
});

test("bytes the server skipped become a dim marker on a line of its own", () => {
  const first = mergeTail(null, answer({ text: "tail\n", start: 5000, next: 5005, end: 5005, gap: 5000 }));
  assert.match(first.text, /^\x1b\[0;2m\[… 4\.9 KB earlier output not shown …\]\x1b\[0m\ntail\n$/);
  let buf = mergeTail(null, answer({ text: "half a line", next: 11, end: 11 }));
  buf = mergeTail(buf, answer({ text: "later\n", start: 111, next: 117, end: 117, gap: 100 }));
  assert.equal(buf.text, "half a line\n\x1b[0;2m[… 100 B not shown …]\x1b[0m\nlater\n");
});

test("an answer that does not continue the buffer starts over", () => {
  const buf = mergeTail(null, answer({ text: "old job\n", next: 900, end: 900 }));
  const again = mergeTail(buf, answer({ text: "new\n", start: 0, next: 4, end: 4 }));
  assert.equal(again.text, "new\n");
  assert.equal(again.next, 4);
});

test("the kept text is capped by lines and by characters, cut at a line start", () => {
  const lines = Array.from({ length: 50 }, (_, i) => `line ${i}`).join("\n") + "\n";
  const byLines = mergeTail(null, answer({ text: lines, next: lines.length }), { maxLines: 10 });
  assert.equal(byLines.text.split("\n").length - 1, 10);
  assert.ok(byLines.text.startsWith("line 40\n") && byLines.trimmed);

  const byChars = mergeTail(null, answer({ text: lines }), { maxChars: 30 });
  assert.ok(byChars.text.length <= 30);
  assert.match(byChars.text, /^line \d+\n/);
  assert.ok(byChars.text.endsWith("line 49\n"));

  const noNewline = mergeTail(null, answer({ text: "x".repeat(100) }), { maxChars: 10 });
  assert.equal(noNewline.text, "x".repeat(10));

  const small = mergeTail(null, answer({ text: "a\nb" }), { maxLines: 2 });
  assert.equal(small.text, "a\nb");
  assert.equal(small.trimmed, false);
});

// ---- formatting ----

test("sizes and durations read at a glance", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(1536), "1.5 KB");
  assert.equal(fmtBytes(3 * 1024 * 1024), "3.0 MB");
  assert.equal(fmtDuration(4.25), "4.3s");
  assert.equal(fmtDuration(125), "2m 05s");
  assert.equal(fmtDuration(3 * 3600 + 7 * 60), "3h 07m");
});
