// "Show me the terminals the AI uses."
//
// Every `bash` call the agent makes already emits a `tool_call` (with the
// command) and a `tool_result` (with the combined output, the duration and
// whether it failed). All of that is in the store already — it was just buried
// three clicks deep in the trajectory, which is a timeline for auditing a run,
// not a place to watch commands go by. This is the same data as a terminal log.
//
// Read-only, and that is the correct shape rather than a limitation: these are
// a *record* of processes that have already exited. There is nothing to type
// into. What the panel offers instead is "run it in my terminal", which pastes
// the command at the live shell's prompt **without a newline** — the model
// wrote that text, so a human keystroke is what executes it, always.

import { store, subscribe } from "../store.js";
import { esc, fmtMs } from "../util.js";
import { renderAnsiBlock } from "./emulator.js";

const MAX_ROWS = 200;

export function initAgentFeed(container, { onRerun }) {
  container.classList.add("qt-feed");
  container.innerHTML = `
    <div class="qt-feed-head">
      <span class="qt-feed-count">No commands yet.</span>
      <label class="qt-feed-follow"><input type="checkbox" checked> follow</label>
    </div>
    <div class="qt-feed-list"></div>`;
  const countEl = container.querySelector(".qt-feed-count");
  const followEl = container.querySelector(".qt-feed-follow input");
  const listEl = container.querySelector(".qt-feed-list");

  const rendered = new Map();   // call id -> row element

  function render() {
    const calls = collect(store.events);
    countEl.textContent = calls.length
      ? `${calls.length} command${calls.length === 1 ? "" : "s"} run by the agent`
      : "No commands yet.";
    // Rebuilt wholesale only when the set changed shape; a finished call just
    // updates its own row, so an open output block does not collapse under you.
    for (const call of calls) {
      const existing = rendered.get(call.key);
      if (!existing) {
        const row = rowEl(call, onRerun);
        rendered.set(call.key, row);
        listEl.appendChild(row);
      } else if (existing.dataset.state !== call.state) {
        const row = rowEl(call, onRerun, existing.classList.contains("open"));
        existing.replaceWith(row);
        rendered.set(call.key, row);
      }
    }
    while (listEl.children.length > MAX_ROWS) {
      const gone = listEl.firstChild;
      rendered.delete(gone.dataset.key);
      gone.remove();
    }
    if (followEl.checked) listEl.scrollTop = listEl.scrollHeight;
  }

  render();
  subscribe((kind, ev) => {
    if (kind === "reset" || kind === "replay_done") { rendered.clear(); listEl.innerHTML = ""; render(); return; }
    if (kind !== "event") return;
    // A replay delivers the whole log one event at a time; re-rendering per
    // event would be quadratic in a session with two hundred commands. The
    // `replay_done` above draws it once, complete.
    if (store.replaying) return;
    const inner = ev.type === "agent_event" ? ev.ev : ev;
    if (!inner) return;
    if ((inner.type === "tool_call" || inner.type === "tool_result") && inner.name === "bash") render();
  });
  return { render };
}

// ---- gathering ----

/** Every bash call in the log, main agent and subagents alike, in order. */
function collect(events) {
  const out = [];
  const byKey = new Map();
  for (const ev of events) {
    const sub = ev.type === "agent_event";
    const inner = sub ? ev.ev : ev;
    if (!inner || inner.name !== "bash") continue;
    const agent = sub ? (ev.agent_id || "subagent") : "";
    const key = `${agent}:${inner.id}`;
    if (inner.type === "tool_call") {
      const call = {
        key, agent, seq: ev.seq,
        state: "running",
        ...parseArgs(inner.arguments),
      };
      byKey.set(key, call);
      out.push(call);
    } else if (inner.type === "tool_result") {
      const call = byKey.get(key);
      if (!call) continue;
      call.state = inner.is_error ? "error" : "done";
      call.output = inner.content || "";
      call.ms = inner.ms || 0;
    }
  }
  return out;
}

function parseArgs(raw) {
  try {
    const obj = typeof raw === "string" ? JSON.parse(raw) : (raw || {});
    return { command: String(obj.command || ""), note: String(obj.description || "") };
  } catch {
    // A call that was interrupted mid-stream has half a JSON object in it.
    return { command: String(raw || "").slice(0, 400), note: "" };
  }
}

// ---- one row ----

const MARK = { running: "…", done: "✓", error: "✕" };

function rowEl(call, onRerun, startOpen = false) {
  const row = document.createElement("div");
  row.className = "qt-cmd" + (startOpen ? " open" : "");
  row.dataset.state = call.state;
  row.dataset.key = call.key;
  const timing = call.ms ? `<span class="qt-cmd-ms">${esc(fmtMs(call.ms))}</span>` : "";
  const who = call.agent ? `<span class="qt-cmd-agent">${esc(call.agent)}</span>` : "";
  row.innerHTML = `
    <div class="qt-cmd-head">
      <span class="qt-cmd-mark qt-${call.state}">${MARK[call.state]}</span>
      ${who}
      <code class="qt-cmd-text">${esc(call.command)}</code>
      ${timing}
      <button class="qt-cmd-rerun" title="Put this command at your own prompt — it will not run until you press Enter">▸ run here</button>
    </div>
    ${call.note ? `<div class="qt-cmd-note">${esc(call.note)}</div>` : ""}
    <div class="qt-cmd-out"></div>`;

  const body = row.querySelector(".qt-cmd-out");
  row.querySelector(".qt-cmd-head").addEventListener("click", (e) => {
    if (e.target.closest(".qt-cmd-rerun")) return;
    const open = row.classList.toggle("open");
    body.innerHTML = open && call.output ? renderAnsiBlock(call.output) : "";
    if (open && !call.output) {
      body.innerHTML = `<div class="qt-cmd-empty">${
        call.state === "running" ? "still running…" : "no output"}</div>`;
    }
  });
  row.querySelector(".qt-cmd-rerun").addEventListener("click", (e) => {
    e.stopPropagation();
    onRerun(call.command);
  });
  if (startOpen && call.output) body.innerHTML = renderAnsiBlock(call.output);
  return row;
}
