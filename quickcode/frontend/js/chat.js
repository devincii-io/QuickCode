// Chat view: incremental transcript renderer over the event store.

import { promptNote } from "./inspect.js";
import { markdownStream, renderMarkdown } from "./markdown.js";
import { midTurn, store, subscribe } from "./store.js";
import { argSummary, markPerm, resultHtml, toolCardNode, traceLink } from "./chat/cards.js";
import { CardRegistry, MAIN } from "./chat/registry.js";
import { Follower } from "./chat/scroll.js";
import { clickable, el, esc, fmtMs, oneLine } from "./util.js";

let transcript, taskStrip;
let welcome = null;             // the empty-conversation greeting, until the first event
let follower = null;            // keeps the newest line in view while the reader is there
let streamNode = null;          // live assistant bubble
let streamMd = null;            // its incremental renderer (markdown.js)
let streamTail = [];            // its nodes the next update replaces
let reasoningText = null;       // the text node inside its <details>
let streamFrame = 0;            // a paint is queued for the next frame
const agentsDirty = new Set();  // subagents whose live text changed since the last frame
// Every card by the ids events refer to it with (tool calls per agent, agent
// cards by agent_id), so no event has to search the transcript for its card.
const cards = new CardRegistry();
let onOpenTrace = () => {};
// The open step: consecutive tool calls collect into one titled block instead
// of stacking as loose cards. Closed by anything that is not a tool call.
let step = null;                // {node, body, title, count, names, block}
// req_id -> {ev, card} for a permission still awaiting its answer. The resolved
// event carries only the verdict, so the request has to be held until then or
// the card cannot show what was actually asked.
let openPerms = new Map();

export function initChat({ openTrace }) {
  transcript = document.getElementById("transcript");
  onOpenTrace = openTrace;
  follower = new Follower(transcript);
  transcript.addEventListener("scroll", () => follower.onScroll(), { passive: true });
  subscribe(onStoreChange);
  clear();
}

function onStoreChange(kind, ev) {
  if (kind === "reset") { clear(); return; }
  if (kind === "status") {
    if (!TERMINAL_STATUS.has(ev.state)) return;
    sweepUnresolved();
    // An interrupt takes the subagents with it (store.js `endOfTurn`), so no
    // card may be left mid-thought.
    if (ev.state === "interrupted") {
      for (const [id, agent] of cards.agents) {
        if (agent.dot.classList.contains("running")) {
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
    dropWelcome();
    // A queued paint belongs before this event on the page: settle it first,
    // so deferring deltas to the next frame never reorders the transcript.
    flushStream();
    renderEvent(ev);
    // A report landing settles the child that wrote it (store.js
    // `settleFromReport`) — the only signal a blocking subagent that died
    // without a closing message ever gets.
    if (ev.type === "tool_result") settleAgentCards();
    scrollBottom(); return;
  }
  if (kind === "stream") {
    dropWelcome();
    schedulePaint();
    return;
  }
  if (kind === "agent_stream") { agentsDirty.add(ev.agent_id); schedulePaint(); return; }
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
function sweepUnresolved(scope = MAIN) {
  for (const entry of cards.runningIn(scope)) {
    cards.settle(entry);
    entry.dot.classList.remove("running");
    entry.dot.classList.add("stale");
    entry.card.querySelector(".tool-head")?.setAttribute(
      "title", "No result: the turn ended before this call finished.");
  }
}

// Agents the store presumes finished (store.js `presumeSettled`): the replayed
// log never said how they ended, so the dot says "stopped, cause unrecorded"
// rather than pulsing as if the work were still going.
function settleAgentCards() {
  for (const [id, agent] of cards.agents) {
    const rec = store.agents.get(id);
    if (!rec?.presumed) continue;
    if (!agent.dot.classList.contains("running")) continue;
    if (rec.status && rec.status !== "done") {
      closeAgentCard({ agent_id: id, status: rec.status });
      continue;
    }
    agent.dot.classList.remove("running");
    agent.dot.classList.add("stale");
    agent.head.setAttribute("title", "No terminal record in the log — presumed finished.");
    sweepUnresolved(id);
  }
}

// A presumed-finished agent that speaks again was alive all along (a detached
// job outliving the reconnect that presumed it over): take the presumption
// back rather than leaving a working agent shown as stopped.
function unpresume(agentId, agent) {
  if (!agent.dot.classList.contains("stale")) return;
  if (store.agents.get(agentId)?.done) return;
  agent.dot.classList.remove("stale");
  agent.dot.classList.add("running");
  agent.head.removeAttribute("title");
}

function clear() {
  if (streamFrame) { cancelAnimationFrame(streamFrame); streamFrame = 0; }
  agentsDirty.clear();
  follower.reset();
  transcript.innerHTML = `<div class="chat-welcome"><span>NEW CONVERSATION</span><h2>What would you like to work on?</h2><p>Describe a change or ask a question about this project.<br>Choose the model and permissions below before sending.</p></div>`;
  welcome = transcript.firstElementChild;
  streamNode = null;
  step = null;
  openPerms = new Map();
  cards.clear();
  taskStrip = null;
}

function dropWelcome() {
  if (!welcome) return;
  welcome.remove();
  welcome = null;
}

// Queued for the next frame (chat/scroll.js), so a burst of events costs one
// layout rather than one each.
function scrollBottom(force = false) {
  if (store.replaying && !force) return;
  follower.request(force);
}

// ---- streaming ----
//
// Deltas arrive far faster than a screen refreshes, and each one used to
// re-parse and re-insert the whole reply: quadratic in its length, with every
// code block rebuilt (and re-decorated by copy.js) dozens of times a second.
// Now a delta only marks the frame dirty, and the paint appends the blocks
// markdown.js has finished and replaces just the open one.

function schedulePaint() {
  if (streamFrame) return;
  streamFrame = requestAnimationFrame(() => paint(true));
}

// `inFrame`: this is the frame's own paint, which may follow the bottom right
// away; flushed early by an event, it leaves that to the frame.
function paint(inFrame = false) {
  streamFrame = 0;
  renderStream();
  for (const id of agentsDirty) renderAgentStream(id);
  agentsDirty.clear();
  if (inFrame && !store.replaying) follower.flush();
  else scrollBottom();
}

function flushStream() {
  if (!streamFrame) return;
  cancelAnimationFrame(streamFrame);
  paint();
}

function ensureStreamNode() {
  if (!streamNode) {
    streamNode = el(`<div class="msg msg-assistant">
        <div class="reasoning-slot"></div><div class="bubble"></div></div>`);
    streamMd = markdownStream();
    streamTail = [];
    reasoningText = null;
    transcript.appendChild(streamNode);
  }
  return streamNode;
}

function fragment(html) {
  const t = document.createElement("template");
  t.innerHTML = html;
  return t.content;
}

function renderStream() {
  if (!store.streamText && !store.streamReasoning && !store.pendingCalls.size) {
    // Emptied without the message that normally replaces it: a replayed
    // message superseded what streamed (store.js). What is on screen is stale.
    if (streamNode) { streamNode.remove(); streamNode = null; }
    return;
  }
  const node = ensureStreamNode();
  if (store.streamReasoning) {
    // Built once and then only its text moves, so collapsing it mid-stream
    // sticks instead of being re-opened by the next delta.
    if (!reasoningText) {
      const details = el(`<details class="reasoning" open><summary>thinking</summary></details>`);
      reasoningText = details.appendChild(document.createTextNode(""));
      node.querySelector(".reasoning-slot").appendChild(details);
    }
    if (reasoningText.data !== store.streamReasoning) reasoningText.data = store.streamReasoning;
  }
  const bubble = node.querySelector(".bubble");
  const { reset, commit, tail } = streamMd.update(store.streamText);
  if (reset) {
    for (const child of [...bubble.childNodes]) {
      if (!child.classList?.contains("copy-btn")) child.remove();
    }
  }
  for (const n of streamTail) n.remove();
  if (commit) bubble.appendChild(fragment(commit));
  const rest = fragment(tail);
  streamTail = [...rest.childNodes];
  bubble.appendChild(rest);
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
    // A hook run says something only when the user should hear it (a refused
    // message, a hook that failed); the rest is trajectory detail.
    case "hook_run":
      return ev.notice ? addNode(el(`<div class="sys-note">⚑ ${esc(ev.notice)}</div>`)) : undefined;
    // The prompt is one line that opens the inspector on its full text;
    // context_injection stays trajectory-only.
    case "system_prompt": return addNode(promptNote(ev));
  }
}

// Drop the live bubble if nothing has been drawn in it. It is created as soon
// as *anything* streams — including a tool call's arguments — so a round that
// went straight to a tool left an empty message behind, once per round, each
// one a stray gap in the transcript. (Its text cannot be the test: copy.js puts
// a "copy" button in every bubble.)
//
// A bubble that has text stays live. Its assistant_message always follows —
// session/recorder.py flushes one before each tool call, at the end of every
// round and on an interrupt — and replaces it. Letting it go here, when an
// unrelated event such as a mode switch landed mid-reply, left the partial copy
// on the page and streamed the whole reply a second time underneath it.
function closeStream() {
  if (streamNode && !streamNode.querySelector(".reasoning, .bubble > :not(.copy-btn)")) {
    streamNode.remove();
    streamNode = null;
  }
}

function addNode(node) {
  closeStream();
  step = null;   // anything that is not a tool call ends the step
  transcript.appendChild(node);
}

// ---- steps ----

// A step is one round's worth of tool calls under a heading. The heading
// names the tools it used: whatever the assistant said is already rendered
// directly above, and repeating it as a title says nothing twice.
function stepTitle(names) {
  const unique = [...new Set(names)];
  if (!unique.length) return "Working";
  const shown = unique.slice(0, 4).join(" · ");
  return unique.length > 4 ? `${shown} +${unique.length - 4}` : shown;
}

function ensureStep() {
  if (step) return step;
  const node = el(`<div class="step">
      <div class="step-head"><span class="step-mark">#</span>
        <span class="step-title"></span>
        <span class="step-count"></span></div>
      <div class="step-body"></div></div>`);
  step = {
    node,
    body: node.querySelector(".step-body"),
    title: node.querySelector(".step-title"),
    count: node.querySelector(".step-count"),
    names: [],                    // what each card's `.tool-name` reads
    block: cards.nextBlock(),
  };
  closeStream();
  transcript.appendChild(node);
  return step;
}

function bumpStepCount(s, name) {
  s.names.push(name);
  const n = s.names.length;
  s.count.textContent = n === 1 ? "1 call" : `${n} calls`;
  s.title.textContent = stepTitle(s.names);
}

function addUser(ev) {
  addNode(el(`<div class="msg msg-user"><div class="bubble">${esc(ev.text)}</div></div>`));
}

function addAssistant(ev) {
  if (streamNode) { streamNode.remove(); streamNode = null; }
  step = null;
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

// A card, registered under the agent whose stream it belongs to.
function newCard(ev, scope, block) {
  const card = toolCardNode(ev, { wireTrace: wireTraceLinks });
  cards.addCall({
    scope, block, card,
    id: ev.id,
    name: String(ev.name ?? ""),   // what its `.tool-name` reads
    args: ev.arguments,
    dot: card.querySelector(".tool-dot"),
  });
  return card;
}

function addToolCall(ev) {
  const s = ensureStep();
  s.body.appendChild(newCard(ev, MAIN, s.block));
  bumpStepCount(s, String(ev.name ?? ""));
}

function settleCard(entry, ev) {
  cards.settle(entry);
  entry.dot.classList.remove("running");
  entry.dot.classList.add(ev.is_error ? "error" : "ok");
  const slot = entry.card.querySelector(".result-slot");
  if (slot) slot.innerHTML = resultHtml(ev.content, ev.is_error);
}

function attachToolResult(ev) {
  const entry = cards.call(ev.id);
  if (!entry) return;
  settleCard(entry, ev);
  if (ev.ms) entry.card.querySelector(".tool-ms").textContent = fmtMs(ev.ms);
  if (ev.is_error) entry.card.classList.add("open");
}

// ---- permissions (the badge itself: chat/cards.js markPerm) ----

function permissionRequested(ev) {
  const entry = permCard(ev);
  openPerms.set(ev.req_id, { ev, entry });
  if (entry) decide(entry, "pending", ev, null);
}

function permissionResolved(ev) {
  const open = openPerms.get(ev.req_id);
  openPerms.delete(ev.req_id);
  // The request is the half that carries the preview and the offered rule, so
  // prefer the card it already found; without it the card is now decided and
  // the undecided-card fallback below would not match.
  const entry = open?.entry || permCard(ev);
  if (!entry) return;
  decide(entry, ev.allow ? "allowed" : "denied", open?.ev || null, ev);
}

function decide(entry, state, reqEv, resEv) {
  cards.decide(entry);
  markPerm(entry.card, state, reqEv, resEv);
}

// Sessions logged before the wire carried `call_id` still replay, so a missing
// id falls back to the one undecided card with the same tool. Ambiguity drops
// the event: a badge on the wrong call is worse than no badge at all.
function permCard(ev) {
  if (ev.call_id) {
    // `agent` names the agent that asked; its own calls are where the id is.
    const scope = ev.agent && ev.agent !== "main" ? ev.agent : MAIN;
    const byId = cards.call(ev.call_id, scope);
    if (byId) return byId;
  }
  const undecided = cards.undecidedFor(ev.tool);
  if (!undecided.length) return null;
  const arg = oneLine(ev.arg, 200);
  return undecided.find((c) => arg && argSummary(c.name, c.args) === arg) || undecided[0];
}

// ---- subagents ----

function addAgentCard(ev) {
  closeStream();
  step = null;
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
  cards.agents.set(ev.agent_id, {
    card, head,
    dot: head.querySelector(".tool-dot"),
    body: card.querySelector(".agent-body"),
    text: card.querySelector(".agent-text"),
    live: card.querySelector(".agent-live"),
    block: cards.nextBlock(),
  });
}

function addAgentEvent(ev) {
  const agent = cards.agents.get(ev.agent_id);
  if (!agent) return;
  unpresume(ev.agent_id, agent);
  const inner = ev.ev || {};
  if (inner.type === "tool_call") {
    agent.body.appendChild(newCard({ ...inner, seq: ev.seq }, ev.agent_id, agent.block));
  } else if (inner.type === "tool_result") {
    const entry = cards.callIn(ev.agent_id, inner.id);
    if (entry) settleCard(entry, inner);
  } else if (inner.type === "assistant_message") {
    // One of these per round that produced text, not one per subagent: append
    // rather than overwrite, or every round but the last is lost. And a round
    // that stopped to call tools has not finished — leaving the dot green there
    // was the card claiming a still-working agent was done.
    const text = agent.text;
    text.textContent = text.textContent ? `${text.textContent}\n\n${inner.text}` : inner.text;
    // The settled text has landed, so the live copy of the same round has to
    // go: the two nodes used to be one, which rendered every round twice and
    // then lost it when the next round's first delta overwrote the lot.
    agent.live.textContent = "";
    if (!midTurn(inner.finish_reason)) closeAgentCard({ agent_id: ev.agent_id });
  }
}

// The card goes terminal. `agent_done` is the only signal a *detached* job
// finishes with — without handling it the roster said "done" while this card
// pulsed "running" for the rest of the session — and a job that was cancelled
// or errored must not read as a green tick.
function closeAgentCard(ev) {
  const agent = cards.agents.get(ev.agent_id);
  if (!agent) return;
  agent.live.textContent = "";
  agent.dot.classList.remove("running");
  agent.dot.classList.add(ev.status && ev.status !== "done" ? "error" : "ok");
  if (ev.status && ev.status !== "done") {
    agent.head.setAttribute("title", `subagent ${ev.status}`);
  }
  // Whatever this agent was still running died with it.
  sweepUnresolved(ev.agent_id);
}

function renderAgentStream(agentId) {
  const agent = cards.agents.get(agentId);
  const a = store.agents.get(agentId);
  if (!agent || !a || a.done) return;
  unpresume(agentId, agent);
  agent.live.textContent = a.streamText;
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
