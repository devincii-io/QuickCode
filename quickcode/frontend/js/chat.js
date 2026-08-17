// Chat view: incremental transcript renderer over the event store.

import { renderMarkdown } from "./markdown.js";
import { store, subscribe } from "./store.js";
import { el, esc, fmtMs, oneLine } from "./util.js";

let transcript, taskStrip;
let streamNode = null;          // live assistant bubble
let agentCards = new Map();     // agent_id -> {card, body, textNode}
let onOpenTrace = () => {};

export function initChat({ openTrace }) {
  transcript = document.getElementById("transcript");
  onOpenTrace = openTrace;
  subscribe(onStoreChange);
}

function onStoreChange(kind, ev) {
  if (kind === "reset") { clear(); return; }
  if (kind === "replay_done") { scrollBottom(true); return; }
  if (kind === "event") { renderEvent(ev); scrollBottom(); return; }
  if (kind === "stream") { renderStream(); scrollBottom(); return; }
  if (kind === "agent_stream") { renderAgentStream(ev.agent_id); return; }
  if (kind === "tasks") { renderTasks(ev.tasks); return; }
  if (kind === "state" && ev.tasks) { renderTasks(ev.tasks); return; }
}

function clear() {
  transcript.innerHTML = "";
  streamNode = null;
  agentCards = new Map();
  taskStrip = null;
}

function scrollBottom(force = false) {
  if (store.replaying && !force) return;
  const nearBottom =
    transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 160;
  if (nearBottom || force) transcript.scrollTop = transcript.scrollHeight;
}

// ---- streaming ----

function ensureStreamNode() {
  if (!streamNode) {
    streamNode = el(`<div class="msg msg-assistant">
        <div class="reasoning-slot"></div><div class="bubble"></div></div>`);
    transcript.appendChild(streamNode);
  }
  return streamNode;
}

function renderStream() {
  if (!store.streamText && !store.streamReasoning && !store.pendingCalls.size) return;
  const node = ensureStreamNode();
  const slot = node.querySelector(".reasoning-slot");
  if (store.streamReasoning) {
    slot.innerHTML = `<details class="reasoning" open><summary>thinking</summary>${
      esc(store.streamReasoning)}</details>`;
  }
  node.querySelector(".bubble").innerHTML = renderMarkdown(store.streamText);
}

// ---- logged events ----

function renderEvent(ev) {
  switch (ev.type) {
    case "user_message": return addUser(ev);
    case "assistant_message": return addAssistant(ev);
    case "tool_call": return addToolCall(ev);
    case "tool_result": return attachToolResult(ev);
    case "system_note": return addNode(el(`<div class="sys-note">${esc(ev.text)}</div>`));
    case "error": return addNode(el(`<div class="err-note">${esc(ev.message)}</div>`));
    case "compacted":
      return addNode(el(`<div class="compact-divider">compacted</div>`));
    case "permission_resolved": {
      const icon = ev.allow ? `<span class="p-ok">✓ allowed</span>`
                            : `<span class="p-no">✗ denied</span>`;
      return addNode(el(`<div class="perm-chip">🔒 <span class="perm-tool">${esc(ev.tool)}</span>
        <span>${esc(oneLine(ev.arg, 80))}</span> ${icon}</div>`));
    }
    case "plan_resolved": {
      const txt = ev.approved ? `plan approved → ${esc(ev.mode_after || "ask")} mode`
                              : "plan sent back for revision";
      return addNode(el(`<div class="sys-note">▤ ${txt}</div>`));
    }
    case "mode_changed":
      return addNode(el(`<div class="sys-note">mode → ${esc(ev.mode)}</div>`));
    case "model_changed":
      return addNode(el(`<div class="sys-note">model → ${esc(ev.model)}</div>`));
    case "agent_spawned": return addAgentCard(ev);
    case "agent_event": return addAgentEvent(ev);
    // system_prompt / context_injection are trajectory-only by design.
  }
}

function addNode(node) { streamNode = null; transcript.appendChild(node); }

function traceLink(seq) {
  return `<span class="trace-link" data-seq="${seq}" title="Open in trajectory">⌕ trace</span>`;
}

function addUser(ev) {
  addNode(el(`<div class="msg msg-user"><div class="bubble">${esc(ev.text)}</div></div>`));
}

function addAssistant(ev) {
  if (streamNode) { streamNode.remove(); streamNode = null; }
  const node = el(`<div class="msg msg-assistant">
      <div class="reasoning-slot"></div>
      <div class="bubble">${renderMarkdown(ev.text)}</div>
      <div class="meta">${traceLink(ev.seq)}</div></div>`);
  if (ev.reasoning) {
    node.querySelector(".reasoning-slot").innerHTML =
      `<details class="reasoning"><summary>thinking</summary>${esc(ev.reasoning)}</details>`;
  }
  wireTraceLinks(node);
  transcript.appendChild(node);
}

// tool-specific one-line argument summaries
function argSummary(name, argsRaw) {
  let a = {};
  try { a = JSON.parse(argsRaw || "{}"); } catch { return oneLine(argsRaw, 120); }
  if (name === "bash") return oneLine(a.command, 140);
  if (name === "read" || name === "write" || name === "edit")
    return oneLine(a.file_path || a.path, 120);
  if (name === "grep") return oneLine(`${a.pattern}  ${a.path || ""}`, 120);
  if (name === "glob") return oneLine(a.pattern, 120);
  if (name === "agent") return oneLine(a.definition || a.prompt, 120);
  const keys = Object.entries(a).map(([k, v]) => `${k}: ${oneLine(String(v), 40)}`);
  return oneLine(keys.join(", "), 140);
}

function diffBody(name, argsRaw) {
  // For edit: show old/new as a colored diff instead of raw JSON.
  if (name !== "edit") return null;
  let a;
  try { a = JSON.parse(argsRaw || "{}"); } catch { return null; }
  if (!a.old_string && !a.new_string) return null;
  const del = String(a.old_string || "").split("\n")
    .map((l) => `<span class="diff-del">- ${esc(l)}</span>`).join("\n");
  const add = String(a.new_string || "").split("\n")
    .map((l) => `<span class="diff-add">+ ${esc(l)}</span>`).join("\n");
  return `<div class="lbl">${esc(a.file_path || "")}</div><pre>${del}\n${add}</pre>`;
}

function toolCardNode(ev, { agent } = {}) {
  const summary = argSummary(ev.name, ev.arguments);
  const card = el(`<div class="tool-card" data-call="${esc(ev.id)}">
    <div class="tool-head">
      <span class="tool-dot running"></span>
      <span class="tool-name">${esc(ev.name)}</span>
      <span class="tool-summary">${esc(summary)}</span>
      <span class="tool-ms"></span>
    </div>
    <div class="tool-body"></div></div>`);
  const body = card.querySelector(".tool-body");
  const diff = diffBody(ev.name, ev.arguments);
  let pretty = ev.arguments;
  try { pretty = JSON.stringify(JSON.parse(ev.arguments || "{}"), null, 2); } catch { /* raw */ }
  body.innerHTML = (diff || `<div class="lbl">Arguments</div><pre>${esc(pretty)}</pre>`) +
    `<div class="result-slot"></div>` +
    (ev.seq != null ? `<div class="lbl" style="margin-top:10px">${traceLink(ev.seq)}</div>` : "");
  card.querySelector(".tool-head").addEventListener("click", () =>
    card.classList.toggle("open"));
  wireTraceLinks(card);
  return card;
}

function addToolCall(ev) {
  streamNode = null;
  transcript.appendChild(toolCardNode(ev));
}

function attachToolResult(ev) {
  const card = transcript.querySelector(`.tool-card[data-call="${CSS.escape(ev.id)}"]`);
  if (!card) return;
  const dot = card.querySelector(".tool-dot");
  dot.classList.remove("running");
  dot.classList.add(ev.is_error ? "error" : "ok");
  if (ev.ms) card.querySelector(".tool-ms").textContent = fmtMs(ev.ms);
  const slot = card.querySelector(".result-slot");
  if (slot) {
    slot.innerHTML = `<div class="lbl">${ev.is_error ? "Error" : "Result"}</div>
      <pre>${esc(ev.content)}</pre>`;
  }
  if (ev.is_error) card.classList.add("open");
}

// ---- subagents ----

function addAgentCard(ev) {
  streamNode = null;
  const card = el(`<div class="agent-card" data-agent="${esc(ev.agent_id)}">
    <div class="agent-head"><span>⛓</span>
      <strong>${esc(ev.agent_id)}</strong>
      <span class="tool-summary">${esc(ev.definition)}</span>
      <span class="tool-dot running"></span></div>
    <div class="agent-body"><div class="agent-text"></div></div></div>`);
  card.querySelector(".agent-head").addEventListener("click", () =>
    card.classList.toggle("open"));
  transcript.appendChild(card);
  agentCards.set(ev.agent_id, card);
}

function addAgentEvent(ev) {
  const card = agentCards.get(ev.agent_id);
  if (!card) return;
  const body = card.querySelector(".agent-body");
  const inner = ev.ev || {};
  if (inner.type === "tool_call") {
    body.appendChild(toolCardNode({ ...inner, seq: ev.seq }, { agent: ev.agent_id }));
  } else if (inner.type === "tool_result") {
    const tc = body.querySelector(`.tool-card[data-call="${CSS.escape(inner.id)}"]`);
    if (tc) {
      const dot = tc.querySelector(".tool-dot");
      dot.classList.remove("running");
      dot.classList.add(inner.is_error ? "error" : "ok");
      const slot = tc.querySelector(".result-slot");
      if (slot) slot.innerHTML =
        `<div class="lbl">${inner.is_error ? "Error" : "Result"}</div><pre>${esc(inner.content)}</pre>`;
    }
  } else if (inner.type === "assistant_message") {
    card.querySelector(".agent-text").textContent = inner.text;
    const dot = card.querySelector(".agent-head .tool-dot");
    dot.classList.remove("running");
    dot.classList.add("ok");
  }
}

function renderAgentStream(agentId) {
  const card = agentCards.get(agentId);
  const a = store.agents.get(agentId);
  if (card && a && !a.done) card.querySelector(".agent-text").textContent = a.streamText;
}

// ---- tasks ----

function renderTasks(tasks) {
  const open = (tasks || []).filter((t) => t.status !== "deleted");
  if (!open.length) { if (taskStrip) { taskStrip.remove(); taskStrip = null; } return; }
  const marks = { completed: "✓", in_progress: "◐", pending: "○" };
  const items = open.map((t) =>
    `<div class="task-item st-${esc(t.status)}"><span class="t-mark">${marks[t.status] || "○"}</span>
     <span>${esc(t.subject)}</span></div>`).join("");
  if (!taskStrip) {
    taskStrip = el(`<div class="task-strip"><div class="t-title">Tasks</div><div class="t-list"></div></div>`);
    transcript.prepend(taskStrip);
  }
  taskStrip.querySelector(".t-list").innerHTML = items;
}

function wireTraceLinks(node) {
  node.querySelectorAll(".trace-link").forEach((l) =>
    l.addEventListener("click", (e) => {
      e.stopPropagation();
      onOpenTrace(Number(l.dataset.seq));
    }));
}
