// The Jobs tab's bookkeeping, apart from the DOM so node:test can reach it.
//
// Two streams describe the same jobs and neither is enough alone. The list
// route (`GET …/jobs`) is the truth about what the table holds, but it is a
// snapshot that can land after a newer event. The socket's `bash_job_*` events
// are prompt but partial: `bash_job_started` cuts the command to 200
// characters, and a window that attached late never saw most of them. So rows
// come from the list, events move them forward in between, and nothing ever
// moves a job backwards — a list answer read before a job ended cannot bring it
// back to life.

export const TAIL_MAX_CHARS = 120_000;
export const TAIL_MAX_LINES = 2_000;

const ENDED = new Set(["exited", "killed"]);

export function jobLabel(job) {
  const desc = String(job.description || "").trim();
  if (desc) return desc;
  const first = String(job.command || "").trim().split("\n")[0] || "";
  return first.slice(0, 80);
}

/** A list row over what was known before it: ends stick, byte counts only grow. */
function settle(prev, row) {
  const next = { ...row, pending: false };
  if (!next.label) next.label = jobLabel(next);
  if (!prev) return next;
  if (ENDED.has(prev.status) && !ENDED.has(next.status)) {
    next.status = prev.status;
    next.exit_code = prev.exit_code;
    next.killed_by = prev.killed_by || "";
    next.seconds = prev.seconds ?? next.seconds;
    next.ended = prev.ended ?? next.ended;
  }
  next.bytes = Math.max(prev.bytes || 0, next.bytes || 0);
  return next;
}

/**
 * Fold a list answer into what the tab holds. A job the list does not name is
 * gone (the table let it go), unless only an event has seen it so far — the
 * list may have been read a moment before that job started.
 */
export function mergeJobs(prev, rows) {
  const out = new Map();
  for (const row of rows || []) {
    if (row && typeof row.id === "string") out.set(row.id, settle(prev.get(row.id), row));
  }
  for (const [id, job] of prev) if (job.pending && !out.has(id)) out.set(id, job);
  return out;
}

/** One row the server just sent (a kill's answer), settled like a list row. */
export function upsertJob(jobs, row) {
  if (!row || typeof row.id !== "string") return jobs;
  const out = new Map(jobs);
  out.set(row.id, settle(jobs.get(row.id), row));
  return out;
}

/** One socket event. Returns the same Map when nothing changed. */
export function applyJobEvent(jobs, ev, nowS = Date.now() / 1000) {
  const id = ev?.job_id;
  if (typeof id !== "string") return jobs;
  const prev = jobs.get(id);
  let next;
  if (ev.type === "bash_job_started") {
    if (prev) return jobs;
    next = {
      id, command: ev.command || "", description: ev.description || "",
      status: "running", exit_code: null, killed_by: "",
      started: nowS, ended: null, seconds: 0, bytes: 0, dropped: 0, pending: true,
    };
    next.label = jobLabel(next);
  } else if (ev.type === "bash_job_done") {
    next = {
      ...(prev || { id, command: "", description: "", label: id, started: null, bytes: 0, pending: true }),
      status: ev.status === "killed" ? "killed" : "exited",
      exit_code: ev.exit_code ?? null,
      killed_by: ev.killed_by || "",
      seconds: ev.seconds ?? prev?.seconds ?? 0,
      ended: prev?.ended ?? nowS,
    };
  } else if (ev.type === "bash_job_output") {
    if (!prev || !(ev.bytes > (prev.bytes || 0))) return jobs;
    next = { ...prev, bytes: ev.bytes };
  } else {
    return jobs;
  }
  const out = new Map(jobs);
  out.set(id, next);
  return out;
}

export function runningCount(jobs) {
  let n = 0;
  for (const job of jobs.values()) if (job.status === "running") n += 1;
  return n;
}

/** Newest first: `bash_12` above `bash_9`, which a string sort gets wrong. */
export function orderJobs(jobs) {
  const num = (id) => Number(String(id).split("_").pop()) || 0;
  return [...jobs.values()].sort((a, b) => num(b.id) - num(a.id));
}

/** Keep the selection while it exists; otherwise the newest running job, else the newest. */
export function pickJob(jobs, selected) {
  if (selected && jobs.has(selected)) return selected;
  const ordered = orderJobs(jobs);
  return (ordered.find((j) => j.status === "running") || ordered[0])?.id ?? null;
}

export function jobChip(job) {
  if (job.status === "running") return { kind: "running", text: "running" };
  if (job.status === "killed") {
    return { kind: "killed", text: job.killed_by === "user" ? "killed by you" : "killed" };
  }
  const code = job.exit_code;
  return { kind: code === 0 ? "ok" : "error", text: `exit ${code ?? "?"}` };
}

export function elapsed(job, nowS = Date.now() / 1000) {
  if (job.status === "running" && job.started) return Math.max(0, nowS - job.started);
  return Number(job.seconds) || 0;
}

export function fmtDuration(s) {
  s = Math.max(0, Number(s) || 0);
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export function fmtBytes(n) {
  n = Math.max(0, Number(n) || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Dim, on a line of its own, and closed with a reset: the emulator draws it
// like any other output, so it sits where the missing bytes were.
function gapLine(bytes, earlier) {
  const what = earlier ? "earlier output not shown" : "not shown";
  return `\x1b[0;2m[… ${fmtBytes(bytes)} ${what} …]\x1b[0m\n`;
}

function capText(text, maxChars, maxLines) {
  let cut = Math.max(0, text.length - maxChars);
  // Walk back over `maxLines` line ends; a final newline ends the last line
  // rather than starting an empty one.
  let at = text.endsWith("\n") ? text.length - 1 : text.length;
  let n = 0;
  while (n < maxLines && at > 0) { at = text.lastIndexOf("\n", at - 1); n++; }
  if (n === maxLines && at >= 0) cut = Math.max(cut, at + 1);
  if (!cut) return { text, trimmed: false };
  // Start on a line of its own when one begins nearby, so what is kept does
  // not open on half a line or half an escape sequence.
  const nl = text.indexOf("\n", cut - 1);
  if (nl >= 0 && nl - cut < 4096) cut = nl + 1;
  return { text: text.slice(cut), trimmed: true };
}

/**
 * Fold one `…/output` answer into the text the view holds.
 *
 * `buf` is `{text, next}` or null; `res` is the route's answer. An answer that
 * does not continue from `buf` (a different job, or a table that restarted
 * under the same id) starts over. Bytes the server skipped become a dim marker
 * line; the kept text is capped by characters and lines, trimmed from the top.
 */
export function mergeTail(buf, res, { maxChars = TAIL_MAX_CHARS, maxLines = TAIL_MAX_LINES } = {}) {
  const fresh = !buf || res.start < buf.next;
  let text = fresh ? "" : buf.text;
  let trimmed = fresh ? false : !!buf.trimmed;
  if (res.gap > 0) {
    if (text && !text.endsWith("\n")) text += "\n";
    text += gapLine(res.gap, fresh);
  }
  text += res.text || "";
  const capped = capText(text, maxChars, maxLines);
  return { text: capped.text, next: res.next, trimmed: trimmed || capped.trimmed };
}
