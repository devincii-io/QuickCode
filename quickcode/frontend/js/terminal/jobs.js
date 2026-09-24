// The terminal drawer's Jobs tab: the agent's background shell jobs, live.
//
// Why the drawer and not a sixth side-panel tab. A job is a process writing a
// terminal log — a dev server, a watcher — and its lines are as wide as the
// shell's, which is the shape this drawer has and a side-panel column does
// not. It is also the continuation of the Agent tab beside it: that one lists
// the commands the agent ran to completion, this one the ones it left running.
//
// What it shows comes from `server/jobs_api.py`: the list of the conversation's
// jobs, and each job's output read by absolute offset. Reading here never moves
// the model's own cursor, so watching a job does not change what `bash_output`
// will call new. The socket says when to read: `bash_job_started` and
// `bash_job_done` (logged) and `bash_job_output` (live-only, a few a second at
// most). Output is read only for the job on screen, and only while the tab is.
//
// Kill is the one thing here that changes anything, so it asks first; the
// server records it as the user's and the agent hears about it next turn.

import { api } from "../api.js";
import { copyText } from "../copy.js";
import { store, subscribe } from "../store.js";
import { toastError, toastOk } from "../toast.js";
import { confirmModal } from "../ui/modal.js";
import { esc, fmtTime } from "../util.js";
import { renderAnsiBlock } from "./emulator.js";
import {
  applyJobEvent, elapsed, fmtBytes, fmtDuration, jobChip, mergeJobs, mergeTail,
  orderJobs, pickJob, runningCount, upsertJob,
} from "./jobs_state.js";

const TAIL_BYTES = 64 * 1024;
// Lines kept in the DOM for the job on screen; older ones stay in the text
// buffer (jobs_state.js caps that) and the ring (server) only.
const DOM_LINES = 1500;
const COLS = 200;

function node(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
}

export function initJobs(container, { onCount } = {}) {
  let jobs = new Map();
  let selected = null;
  let buf = null;              // {id, text, next, trimmed} for the job on screen
  let meta = null;             // the last output answer's status and counters
  let visible = false;
  let listGen = 0;
  let pulling = false;
  let pullAgain = false;
  let paintQueued = false;
  let jumpToEnd = true;
  let ticker = null;
  const rows = new Map();      // job id -> row button

  container.classList.add("qt-jobs");
  const listEl = node("div", "qt-jobs-list");
  listEl.setAttribute("role", "listbox");
  listEl.setAttribute("aria-label", "Background jobs");
  const detail = node("div", "qt-jobs-detail");
  const head = node("div", "qt-jobs-head");
  const chipEl = node("span", "qt-job-chip");
  const cmdEl = node("code", "qt-jobs-cmd");
  const copyBtn = node("button", "qt-jobs-btn", "⧉ Copy command");
  copyBtn.title = "Copy the full command";
  const killBtn = node("button", "qt-jobs-btn qt-jobs-kill", "✕ Kill");
  killBtn.title = "Kill this job and every process it started";
  head.append(chipEl, cmdEl, copyBtn, killBtn);
  const metaEl = node("div", "qt-jobs-meta");
  const outEl = node("div", "qt-jobs-out");
  outEl.setAttribute("role", "log");
  outEl.setAttribute("aria-label", "Job output");
  outEl.tabIndex = 0;
  detail.append(head, metaEl, outEl);
  const emptyEl = node("div", "qt-jobs-empty");
  emptyEl.append(
    node("div", "qt-jobs-empty-title", "No background jobs in this conversation."),
    node("div", "qt-jobs-empty-hint",
      "When the agent starts a command with run_in_background — a dev server, a " +
      "watcher, a long build — it is listed here with its live output and a Kill button."),
  );
  container.append(emptyEl, listEl, detail);

  // ---- data ----

  async function refresh() {
    const conv = store.convId;
    if (!conv) return;
    const gen = ++listGen;
    let res;
    try {
      res = await api.jobs(conv);
    } catch {
      return;   // not open (yet): the next event or replay asks again
    }
    if (gen !== listGen || conv !== store.convId) return;
    jobs = mergeJobs(jobs, res.jobs);
    render();
  }

  async function pull() {
    if (!visible || !selected || !store.convId) return;
    if (pulling) { pullAgain = true; return; }
    pulling = true;
    const id = selected;
    const conv = store.convId;
    try {
      const own = buf && buf.id === id ? buf : null;
      const res = await api.jobOutput(conv, id, own ? own.next : 0, TAIL_BYTES);
      if (id !== selected || conv !== store.convId) return;
      buf = { id, ...mergeTail(own, res) };
      meta = res;
      if (res.status !== "running" && jobs.get(id)?.status === "running") refresh();
      paint();
    } catch (err) {
      if (id === selected) {
        meta = null;
        metaEl.textContent = `Could not read ${id}'s output: ${err.message}`;
      }
    } finally {
      pulling = false;
      if (pullAgain) { pullAgain = false; pull(); }
    }
  }

  // ---- rendering ----

  function render() {
    const pick = pickJob(jobs, selected);
    if (pick !== selected) choose(pick);
    const ordered = orderJobs(jobs);
    container.classList.toggle("qt-jobs-none", ordered.length === 0);
    for (const [id, row] of rows) {
      if (!jobs.has(id)) { row.remove(); rows.delete(id); }
    }
    ordered.forEach((job, i) => {
      let row = rows.get(job.id);
      if (!row) {
        row = rowEl(job.id);
        rows.set(job.id, row);
      }
      fillRow(row, job);
      if (listEl.children[i] !== row) listEl.insertBefore(row, listEl.children[i] || null);
    });
    renderHead();
    if (onCount) onCount(runningCount(jobs));
    syncTicker();
  }

  function rowEl(id) {
    const row = node("button", "qt-job");
    row.type = "button";
    row.dataset.id = id;
    row.setAttribute("role", "option");
    row.append(
      node("span", "qt-job-chip"), node("span", "qt-job-id"),
      node("span", "qt-job-label"), node("span", "qt-job-meta"),
    );
    row.addEventListener("click", () => select(id));
    return row;
  }

  function fillRow(row, job) {
    const [chip, idEl, label, metaSpan] = row.children;
    const c = jobChip(job);
    chip.className = `qt-job-chip qt-chip-${c.kind}`;
    chip.textContent = c.text;
    idEl.textContent = job.id;
    label.textContent = job.label || job.command || "";
    row.title = job.command || "";
    metaSpan.textContent = `${fmtDuration(elapsed(job))} · ${fmtBytes(job.bytes)}`;
    const on = job.id === selected;
    row.classList.toggle("active", on);
    row.setAttribute("aria-selected", on ? "true" : "false");
  }

  function renderHead() {
    const job = selected ? jobs.get(selected) : null;
    detail.hidden = !job;
    if (!job) return;
    const c = jobChip(job);
    chipEl.className = `qt-job-chip qt-chip-${c.kind}`;
    chipEl.textContent = c.text;
    cmdEl.textContent = job.command || job.label || job.id;
    cmdEl.title = job.command || "";
    killBtn.hidden = job.status !== "running";
    const bits = [job.id];
    if (job.description) bits.push(job.description);
    if (job.started) bits.push(`started ${fmtTime(job.started * 1000)}`);
    bits.push(fmtDuration(elapsed(job)));
    bits.push(`${fmtBytes(Math.max(job.bytes || 0, meta?.end || 0))} written`);
    if (meta && meta.id === job.id) {
      if (meta.dropped) bits.push(`oldest ${fmtBytes(meta.dropped)} no longer kept`);
      bits.push(meta.unread ? `${fmtBytes(meta.unread)} not yet read by the agent`
        : "the agent has read all of it");
    }
    if (job.status === "running" && job.exit_code != null) {
      bits.push(`its shell exited ${job.exit_code}; something it started still runs`);
    }
    metaEl.textContent = bits.join(" · ");
  }

  function paint() {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => {
      paintQueued = false;
      const atEnd = outEl.scrollHeight - outEl.scrollTop - outEl.clientHeight < 24;
      if (buf && buf.id === selected && buf.text) {
        outEl.innerHTML = renderAnsiBlock(buf.text, COLS, DOM_LINES);
      } else {
        outEl.replaceChildren(node("div", "qt-cmd-empty",
          jobs.get(selected)?.status === "running" ? "No output yet…" : "No output."));
      }
      // Follow the end unless the reader scrolled up to look at something.
      if (jumpToEnd || atEnd) outEl.scrollTop = outEl.scrollHeight;
      jumpToEnd = false;
      renderHead();
    });
  }

  // A different job on screen starts its output from scratch: the newest
  // TAIL_BYTES, with what came before marked as not shown.
  function choose(id) {
    selected = id;
    buf = null;
    meta = null;
    jumpToEnd = true;
    outEl.replaceChildren();
    if (id) queueMicrotask(pull);
  }

  function select(id) {
    if (id === selected) return;
    choose(id);
    render();
  }

  // Durations of running jobs count up while someone can see them.
  function syncTicker() {
    const want = visible && runningCount(jobs) > 0;
    if (want && !ticker) {
      ticker = setInterval(() => {
        for (const [id, row] of rows) {
          const job = jobs.get(id);
          if (job?.status === "running") fillRow(row, job);
        }
        renderHead();
      }, 1000);
    } else if (!want && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  // ---- actions ----

  copyBtn.addEventListener("click", () => {
    const job = jobs.get(selected);
    if (job?.command) copyText(job.command, job.command_truncated ? "Copied the first 16 KB of the command" : "Command copied");
  });

  killBtn.addEventListener("click", async () => {
    const job = jobs.get(selected);
    if (!job || job.status !== "running") return;
    const ok = await confirmModal({
      title: `Kill ${job.id}?`,
      body: `<p>This stops the command and every process it started. What it
        has written so far stays readable, and the agent is told it was killed
        at the start of its next turn.</p>
        <pre class="qt-jobs-confirm">${esc(job.command || job.label)}</pre>`,
      confirm: "Kill job",
    });
    if (!ok) return;
    killBtn.disabled = true;
    try {
      const res = await api.killJob(store.convId, job.id);
      jobs = upsertJob(jobs, res.job);
      toastOk(res.killed ? `Killed ${job.id}` : `${job.id} had already ended`);
      render();
      pull();
    } catch (err) {
      toastError(`Could not kill ${job.id}: ${err.message}`);
    } finally {
      killBtn.disabled = false;
      refresh();
    }
  });

  // ---- the socket ----

  subscribe((kind, ev) => {
    if (kind === "reset") {
      jobs = new Map();
      choose(null);
      listGen++;
      render();
      return;
    }
    if (kind === "replay_done") { refresh(); return; }
    if (kind === "event") {
      // A replayed log describes jobs from before this server started, which
      // died with it; the list asked for at replay_done is what exists now.
      if (store.replaying) return;
      if (ev.type !== "bash_job_started" && ev.type !== "bash_job_done") return;
      jobs = applyJobEvent(jobs, ev);
      render();
      refresh();
      if (ev.type === "bash_job_done" && ev.job_id === selected) pull();
      return;
    }
    if (kind === "misc" && ev?.type === "bash_job_output") {
      const before = jobs;
      jobs = applyJobEvent(jobs, ev);
      const row = rows.get(ev.job_id);
      if (jobs !== before && row) fillRow(row, jobs.get(ev.job_id));
      if (ev.job_id === selected) pull();
    }
  });

  render();

  return {
    setVisible(on) {
      if (on === visible) return;
      visible = on;
      syncTicker();
      if (on) {
        refresh();
        jumpToEnd = true;
        pull();
      }
    },
  };
}
