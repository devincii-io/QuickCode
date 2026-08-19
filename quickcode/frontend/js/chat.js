// Chat view: incremental transcript renderer over the event store.

import { renderMarkdown } from "./markdown.js";
import { midTurn, store, subscribe } from "./store.js";
import { renderAnsiBlock } from "./terminal/emulator.js";
import { highlightToon, toon } from "./toon.js";
import { configTarget } from "./trajectory.js";
import { clickable, el, esc, fmtMs, oneLine } from "./util.js";

let transcript, taskStrip;
let streamNode = null;          // live assistant bubble
let agentCards = new Map();     // agent_id -> {card, body, textNode}
let onOpenTrace = () => {};
// The open step: consecutive tool calls collect into one titled block instead
// of stacking as loose cards. Closed by anything that is not a tool call.
let stepNode = null;
let lastAssistantText = "";
// req_id -> {ev, card} for a permission still awaiting its answer. The resolved
// event carries only the verdict, so the request has to be held until then or
// the card cannot show what was actually asked.
let openPerms = new Map();

export function initChat({ openTrace }) {
  transcript = document.getElementById("transcript");
  onOpenTrace = openTrace;
  subscribe(onStoreChange);
}

function onStoreChange(kind, ev) {
  if (kind === "reset") { clear(); return; }
  if (kind === "status") {
    if (!TERMINAL_STATUS.has(ev.state)) return;
    sweepUnresolved();
    // An interrupt takes the subagents with it (store.js `endOfTurn`), so no
    // card may be left mid-thought.
    if (ev.state === "interrupted") {
      for (const [id, card] of agentCards) {
        if (card.querySelector(".agent-head .tool-dot.running")) {
          closeAgentCard({ agent_id: id, status: "interrupted" });
        }
      }
    }
    return;
  }
  if (kind === "replay_done") {
    // A log replays without the live status events, so an interrupted turn
    // would spin its cut-off tool calls again on every reconnect. `busy` is
    // the state event the server sends before the replay, and it is what says
    // whether anything in that log is still in flight.
    if (store.state && !store.state.busy) { sweepUnresolved(); settleAgentCards(); }
    scrollBottom(true); return;
  }
  if (kind === "event") {
    renderEvent(ev);
    // A report landing settles the child that wrote it (store.js
    // `settleFromReport`) — the only signal a blocking subagent that died
    // without a closing message ever gets.
    if (ev.type === "tool_result") settleAgentCards();
    scrollBottom(); return;
  }
  if (kind === "stream") { renderStream(); scrollBottom(); return; }
  if (kind === "agent_stream") { renderAgentStream(ev.agent_id); return; }
  if (kind === "tasks") { renderTasks(ev.tasks); return; }
  if (kind === "state" && ev.tasks) { renderTasks(ev.tasks); return; }
}

// The states the agent loop stops in; see store.js. Nothing of the main
// agent's can still be running once one of them arrives.
const TERMINAL_STATUS = new Set(["idle", "interrupted", "error"]);

// A tool call cancelled by an interrupt is answered in the message history and
// emits no `tool_result`, so its dot pulsed "running" for the rest of the
// session — the transcript claiming work was in flight long after the turn
// ended. Subagent cards are left alone here: a detached job outlives the main
// turn, and each card sweeps itself when *it* goes terminal.
function sweepUnresolved(root = transcript) {
  if (!root) return;
  for (const dot of root.querySelectorAll(".tool-dot.running")) {
    const card = dot.closest(".tool-card");
    if (!card) continue;                                  // an agent head dot
    if (root === transcript && card.closest(".agent-card")) continue;
    dot.classList.remove("running");
    dot.classList.add("stale");
    card.querySelector(".tool-head")?.setAttribute(
      "title", "No result: the turn ended before this call finished.");
  }
}

// Agents the store presumes finished (store.js `presumeSettled`): the replayed
// log never said how they ended, so the dot says "stopped, cause unrecorded"
// rather than pulsing as if the work were still going.
function settleAgentCards() {
  for (const [id, card] of agentCards) {
    const rec = store.agents.get(id);
    if (!rec?.presumed) continue;
    const dot = card.querySelector(".agent-head .tool-dot");
    if (!dot.classList.contains("running")) continue;
    if (rec.status && rec.status !== "done") {
      closeAgentCard({ agent_id: id, status: rec.status });
      continue;
    }
    dot.classList.remove("running");
    dot.classList.add("stale");
    card.querySelector(".agent-head")
      .setAttribute("title", "No terminal record in the log — presumed finished.");
    sweepUnresolved(card);
  }
}

// A presumed-finished agent that speaks again was alive all along (a detached
// job outliving the reconnect that presumed it over): take the presumption
// back rather than leaving a working agent shown as stopped.
function unpresume(agentId, card) {
  const dot = card.querySelector(".agent-head .tool-dot");
  if (!dot.classList.contains("stale")) return;
  if (store.agents.get(agentId)?.done) return;
  dot.classList.remove("stale");
  dot.classList.add("running");
  card.querySelector(".agent-head").removeAttribute("title");
}

function clear() {
  transcript.innerHTML = "";
  streamNode = null;
  stepNode = null;
  lastAssistantText = "";
  openPerms = new Map();
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
    case "permission_request": return permissionRequested(ev);
    case "permission_resolved": return permissionResolved(ev);
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
    case "agent_done": return closeAgentCard(ev);
    // system_prompt / context_injection are trajectory-only by design.
  }
}

// Let go of the live bubble. It is created as soon as *anything* streams —
// including a tool call's arguments — so a round that went straight to a tool
// left an empty message behind, once per round, each one a stray gap in the
// transcript. Text already streamed is kept: it is what the model said.
function closeStream() {
  if (streamNode && !streamNode.textContent.trim()) streamNode.remove();
  streamNode = null;
}

function addNode(node) {
  closeStream();
  stepNode = null;   // anything that is not a tool call ends the step
  transcript.appendChild(node);
}

// ---- steps ----

// A step is one round's worth of tool calls under a heading. The heading
// names the tools it used: whatever the assistant said is already rendered
// directly above, and repeating it as a title says nothing twice.
function stepTitle(step) {
  const names = [...step.querySelectorAll(".tool-name")].map((n) => n.textContent);
  const unique = [...new Set(names)];
  if (!unique.length) return "Working";
  const shown = unique.slice(0, 4).join(" · ");
  return unique.length > 4 ? `${shown} +${unique.length - 4}` : shown;
}

function ensureStep() {
  if (stepNode) return stepNode;
  stepNode = el(`<div class="step">
      <div class="step-head"><span class="step-mark">#</span>
        <span class="step-title"></span>
        <span class="step-count"></span></div>
      <div class="step-body"></div></div>`);
  closeStream();
  transcript.appendChild(stepNode);
  return stepNode;
}

function bumpStepCount(step) {
  const n = step.querySelectorAll(".tool-card").length;
  step.querySelector(".step-count").textContent = n === 1 ? "1 call" : `${n} calls`;
  step.querySelector(".step-title").textContent = stepTitle(step);
}

function traceLink(seq) {
  return `<span class="trace-link" data-seq="${seq}" title="Open in trajectory">⌕ trace</span>`;
}

function addUser(ev) {
  lastAssistantText = "";
  addNode(el(`<div class="msg msg-user"><div class="bubble">${esc(ev.text)}</div></div>`));
}

function addAssistant(ev) {
  if (streamNode) { streamNode.remove(); streamNode = null; }
  stepNode = null;
  lastAssistantText = ev.text || "";
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

// Tools that act on a path: the summary becomes a copyable file reference.
const PATH_TOOLS = new Set(["read", "write", "edit"]);

function summaryHtml(name, argsRaw) {
  const summary = argSummary(name, argsRaw);
  if (!PATH_TOOLS.has(name) || !summary) return esc(summary);
  return `<span class="file-ref" data-path="${esc(summary)}" title="Copy path"
    role="button" tabindex="0">${esc(summary)}</span>`;
}

// The card that governs this tool — the same target the trajectory links to,
// resolved by the same function so the two views cannot drift about which page
// decides what a tool may do.
function configLinkHtml(ev) {
  const target = configTarget(ev);
  if (!target) return "";
  return `<a class="tool-ms k-link" href="${esc(target.href)}"
    title="Open ${esc(target.label)} in configuration — this is what governs it"
    >${esc(target.label)} ↗</a>`;
}

function toolCardNode(ev, { agent } = {}) {
  const card = el(`<div class="tool-card" data-call="${esc(ev.id)}">
    <div class="tool-head" aria-expanded="false">
      <span class="tool-dot running"></span>
      <span class="tool-name">${esc(ev.name)}</span>
      <span class="tool-summary">${summaryHtml(ev.name, ev.arguments)}</span>
      ${configLinkHtml(ev)}
      <span class="tool-ms"></span>
    </div>
    <div class="tool-body"></div></div>`);
  const body = card.querySelector(".tool-body");
  const diff = diffBody(ev.name, ev.arguments);
  const input = diff ? `<div class="io-diff">${diff}</div>` : toonBlock(ev.arguments);
  body.innerHTML =
    `<details class="io io-in"><summary><span class="io-tag">IN</span>
       <span class="io-hint">arguments</span></summary>${input}</details>` +
    `<div class="result-slot"></div>` +
    (ev.seq != null ? `<div class="io-trace">${traceLink(ev.seq)}</div>` : "");
  const head = card.querySelector(".tool-head");
  clickable(head, (e) => {
    // The head toggles the card, but it now contains a link. Without this the
    // link both navigates and collapses the card behind it.
    if (e?.target?.closest?.("a")) return;
    head.setAttribute("aria-expanded", String(card.classList.toggle("open")));
  });
  wireFileRefs(card);
  wireTraceLinks(card);
  return card;
}

// A JSON payload as TOON — the encoding the model was actually handed, so the
// card shows what was sent rather than a JSON re-render of it. The model's
// arguments arrive as a string it wrote, which can be truncated or malformed,
// and a tool result is only sometimes JSON: anything that will not parse falls
// back to the raw text, because losing the call is worse than losing the shape.
// Escape bytes, or a carriage return doing an in-place redraw. `bash` cleans
// its own output, so what reaches here is from a tool the boundary does not
// control: an MCP server, a plugin. Drawn as text those are line noise, and a
// `\r` progress bar is a few hundred near-identical lines. The panel's
// emulator already knows what a terminal would have shown, so use it.
const ANSI_RE = /\x1b[[\]()]|\r[^\n]/;   // eslint-disable-line no-control-regex

function toonBlock(raw) {
  const text = String(raw ?? "");
  if (ANSI_RE.test(text)) {
    return `<pre class="code ansi">${renderAnsiBlock(text)}</pre>`;
  }
  const head = text.trim()[0];
  // Only an object or an array is worth re-encoding. Without this a bash
  // result that happens to be the single word `12345` would parse as JSON and
  // come back through TOON with its whitespace quietly rewritten.
  if (head !== "{" && head !== "[") return `<pre class="code">${esc(text)}</pre>`;
  let parsed;
  try { parsed = JSON.parse(text); } catch { return `<pre class="code">${esc(text)}</pre>`; }
  const encoded = toon(parsed);
  // `{}` and `[]` encode to nothing at all. Showing the two characters the
  // model sent says more than an empty box.
  if (!encoded.trim()) return `<pre class="code">${esc(text)}</pre>`;
  return `<pre class="toon">${highlightToon(encoded)}</pre>`;
}

function resultHtml(content, isError) {
  const text = String(content ?? "");
  const pane = toonBlock(text);
  const tag = isError ? "ERR" : "OUT";
  return `<details class="io io-out${isError ? " io-error" : ""}"${isError ? " open" : ""}>
      <summary><span class="io-tag">${tag}</span>
      <span class="io-hint">${isError ? "error" : "result"}</span></summary>${pane}</details>`;
}

// Clicking a path copies it: there is no editor to open it in, and a link
// that silently does nothing is worse than one that does something small.
function wireFileRefs(node) {
  node.querySelectorAll(".file-ref").forEach((ref) => {
    clickable(ref, (e) => {
      e?.stopPropagation?.();
      const path = ref.dataset.path || "";
      navigator.clipboard?.writeText(path).then(() => {
        ref.classList.add("copied");
        setTimeout(() => ref.classList.remove("copied"), 900);
      }, () => { /* clipboard blocked: leave the text selectable */ });
    });
  });
}

function addToolCall(ev) {
  const step = ensureStep();
  step.querySelector(".step-body").appendChild(toolCardNode(ev));
  bumpStepCount(step);
}

function attachToolResult(ev) {
  const card = transcript.querySelector(`.tool-card[data-call="${CSS.escape(ev.id)}"]`);
  if (!card) return;
  const dot = card.querySelector(".tool-dot");
  dot.classList.remove("running");
  dot.classList.add(ev.is_error ? "error" : "ok");
  if (ev.ms) card.querySelector(".tool-ms").textContent = fmtMs(ev.ms);
  const slot = card.querySelector(".result-slot");
  if (slot) slot.innerHTML = resultHtml(ev.content, ev.is_error);
  if (ev.is_error) card.classList.add("open");
}

// ---- permissions ----
//
// A decision belongs to the call it gates. It used to render as a chip of its
// own under the card, which repeated the tool and the argument already on the
// card and — being a node that is not a tool call — closed the step the card
// was sitting in, so every gated call split its round in two. The badge and the
// detail block below live inside the card instead.

const LOCKS = {
  pending: { glyph: "🔒", hint: "waiting for your decision" },
  allowed: { glyph: "🔓", hint: "allowed" },
  // The struck lock, not a bare ✗: the glyph has to keep saying "permission"
  // next to a status dot that is already red for a failed call.
  denied: { glyph: "🔒", hint: "denied" },
};

function permissionRequested(ev) {
  const card = permCard(ev);
  openPerms.set(ev.req_id, { ev, card });
  if (card) markPerm(card, "pending", ev, null);
}

function permissionResolved(ev) {
  const open = openPerms.get(ev.req_id);
  openPerms.delete(ev.req_id);
  // The request is the half that carries the preview and the offered rule, so
  // prefer the card it already found; without it the card is now decided and
  // the undecided-card fallback below would not match.
  const card = open?.card || permCard(ev);
  if (!card) return;
  markPerm(card, ev.allow ? "allowed" : "denied", open?.ev || null, ev);
}

// Sessions logged before the wire carried `call_id` still replay, so a missing
// id falls back to the one undecided card with the same tool. Ambiguity drops
// the event: a badge on the wrong call is worse than no badge at all.
function permCard(ev) {
  if (ev.call_id) {
    const byId = transcript.querySelector(`.tool-card[data-call="${CSS.escape(ev.call_id)}"]`);
    if (byId) return byId;
  }
  const undecided = [...transcript.querySelectorAll(".tool-card:not([data-perm])")]
    .filter((c) => c.querySelector(".tool-name")?.textContent === ev.tool);
  if (!undecided.length) return null;
  const arg = oneLine(ev.arg, 200);
  return undecided.find((c) => arg && c.querySelector(".tool-summary")?.textContent === arg)
    || undecided[0];
}

function markPerm(card, state, reqEv, resEv) {
  card.dataset.perm = state;
  const lock = LOCKS[state];
  let badge = card.querySelector(".tool-lock");
  if (!badge) {
    badge = el(`<span class="tool-lock"></span>`);
    card.querySelector(".tool-head .tool-dot").after(badge);
  }
  badge.textContent = lock.glyph;
  badge.title = `Permission — ${lock.hint}`;
  const body = card.querySelector(".tool-body");
  let block = body.querySelector(".io-perm");
  if (!block) {
    block = el(`<details class="io io-perm"><summary><span class="io-tag">ASK</span>
      <span class="io-hint"></span></summary><div class="perm-detail"></div></details>`);
    body.insertBefore(block, body.querySelector(".result-slot"));
  }
  block.querySelector(".io-hint").textContent = `permission — ${lock.hint}`;
  block.querySelector(".perm-detail").innerHTML = permDetailHtml(reqEv, resEv);
  // Open for the two states that want an answer or explain a failure, closed
  // for a plain "allowed" — the same call `io-error` makes one block down.
  block.open = state !== "allowed";
}

function permDetailHtml(reqEv, resEv) {
  const verdict = !resEv ? "waiting for your decision"
    : resEv.allow
      ? (resEv.persist ? "allowed, and remembered as a rule" : "allowed, this once")
      : "denied";
  const rows = [`<div class="lbl">decision</div><div class="perm-line">${esc(verdict)}</div>`];
  // The preview is the tool's own rendering of the call and the argument is
  // what the rules matched on; showing both put the same string on screen
  // twice for every path tool. The preview wins when there is one.
  const asked = reqEv?.preview || reqEv?.arg || resEv?.arg || "";
  if (asked) rows.push(`<div class="lbl">what it asked to do</div><pre>${esc(asked)}</pre>`);
  if (reqEv?.rule_suggestion) {
    rows.push(`<div class="lbl">rule offered</div>
      <div class="perm-line perm-code">${esc(reqEv.rule_suggestion)}</div>`);
  }
  if (reqEv?.agent && reqEv.agent !== "main") {
    rows.push(`<div class="lbl">agent</div><div class="perm-line">${esc(reqEv.agent)}</div>`);
  }
  return rows.join("");
}

// ---- subagents ----

function addAgentCard(ev) {
  closeStream();
  stepNode = null;
  const card = el(`<div class="agent-card" data-agent="${esc(ev.agent_id)}">
    <div class="agent-head" aria-expanded="false"><span>⛓</span>
      <strong>${esc(ev.agent_id)}</strong>
      <span class="tool-summary">${esc(ev.definition)}</span>
      <span class="tool-dot running"></span></div>
    <div class="agent-body"><div class="agent-text"></div>
      <div class="agent-live"></div></div></div>`);
  const head = card.querySelector(".agent-head");
  clickable(head, () => {
    head.setAttribute("aria-expanded", String(card.classList.toggle("open")));
  });
  transcript.appendChild(card);
  agentCards.set(ev.agent_id, card);
}

function addAgentEvent(ev) {
  const card = agentCards.get(ev.agent_id);
  if (!card) return;
  unpresume(ev.agent_id, card);
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
      if (slot) slot.innerHTML = resultHtml(inner.content, inner.is_error);
    }
  } else if (inner.type === "assistant_message") {
    // One of these per round that produced text, not one per subagent: append
    // rather than overwrite, or every round but the last is lost. And a round
    // that stopped to call tools has not finished — leaving the dot green there
    // was the card claiming a still-working agent was done.
    const text = card.querySelector(".agent-text");
    text.textContent = text.textContent ? `${text.textContent}\n\n${inner.text}` : inner.text;
    // The settled text has landed, so the live copy of the same round has to
    // go: the two nodes used to be one, which rendered every round twice and
    // then lost it when the next round's first delta overwrote the lot.
    card.querySelector(".agent-live").textContent = "";
    if (!midTurn(inner.finish_reason)) closeAgentCard({ agent_id: ev.agent_id });
  }
}

// The card goes terminal. `agent_done` is the only signal a *detached* job
// finishes with — without handling it the roster said "done" while this card
// pulsed "running" for the rest of the session — and a job that was cancelled
// or errored must not read as a green tick.
function closeAgentCard(ev) {
  const card = agentCards.get(ev.agent_id);
  if (!card) return;
  card.querySelector(".agent-live").textContent = "";
  const dot = card.querySelector(".agent-head .tool-dot");
  dot.classList.remove("running");
  dot.classList.add(ev.status && ev.status !== "done" ? "error" : "ok");
  if (ev.status && ev.status !== "done") {
    card.querySelector(".agent-head").setAttribute("title", `subagent ${ev.status}`);
  }
  // Whatever this agent was still running died with it.
  sweepUnresolved(card);
}

function renderAgentStream(agentId) {
  const card = agentCards.get(agentId);
  const a = store.agents.get(agentId);
  if (!card || !a || a.done) return;
  unpresume(agentId, card);
  card.querySelector(".agent-live").textContent = a.streamText;
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
