// Agents panel: live roster of spawned subagents (the old TUI "FleetView").
// One card per subagent with status, tool count, running duration, and an
// expandable mini-transcript (tool calls + result, final/streaming text).
//
// INTEGRATION
//   import { panel as agentsPanel } from "./panels/agents.js";
//   agentsPanel.init(containerDiv);           // call exactly once
// Assumptions the host must honour:
//   - `init(container)` is called at most once, with a container this module
//     owns outright (it sets innerHTML and adds the `.panel-agents` class).
//   - The container has a definite height (it is a flex column; the card list
//     is the scroller). A `height:100%` / flex:1 parent is enough.
//   - css/panels.css is linked; every selector here is namespaced under
//     `.panel-agents`.
//   - init() may run before the WebSocket connects; the panel renders from
//     `store` and re-renders on every relevant notification, so mounting it
//     hidden and revealing it later needs no extra call.

import { store, subscribe } from "../store.js";
import { el, esc, fmtMs, oneLine } from "../util.js";

let root = null;
const openIds = new Set();     // agent_id -> card expanded
const openCalls = new Set();   // "agent_id::call_id" -> tool card expanded
const autoOpened = new Set();  // error cards already auto-expanded once
const times = new Map();       // agent_id -> {start, end}
let ticker = null;
let frame = 0;

export const panel = {
  id: "agents",
  title: "Agents",
  icon: "⛓",
  init(container) {
    root = container;
    root.classList.add("panel-agents");
    root.innerHTML = `<div class="pa-summary"></div><div class="pa-list"></div>`;
    subscribe(onStoreChange);
    render();
  },
};

function onStoreChange(kind, ev) {
  if (kind === "reset") {
    openIds.clear(); openCalls.clear(); autoOpened.clear(); times.clear();
    return schedule();
  }
  if (kind === "replay_done") return schedule();
  if (kind === "event") {
    if (ev.type === "agent_spawned" || ev.type === "agent_event" || ev.type === "agent_done") {
      schedule();
    }
    return;
  }
  if (kind === "agent_stream") return renderAgentStream(ev.agent_id);
}

// Coalesce bursts of agent events into one repaint.
function schedule() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}

// ---- model ----

// Rebuild the whole roster from the event log; cheap at this scale and
// immune to replay/reconnect ordering.
function buildAgents() {
  const map = new Map();
  const get = (id, definition, ts) => {
    let a = map.get(id);
    if (!a) { a = { id, definition: definition || "", ts, events: [] }; map.set(id, a); }
    if (definition && !a.definition) a.definition = definition;
    return a;
  };
  for (const ev of store.events) {
    if (ev.type === "agent_spawned") {
      get(ev.agent_id, ev.definition, ev.ts).ts ??= ev.ts;
    } else if (ev.type === "agent_event" && ev.ev) {
      get(ev.agent_id).events.push({ ...ev.ev, ts: ev.ts });
    } else if (ev.type === "agent_done" && ev.agent_id) {
      // Not emitted by the server today, but loggable — honour it as terminal.
      const a = get(ev.agent_id);
      a.closed = true;
      a.closedTs = ev.ts;
    }
  }
  for (const [id, rec] of store.agents) get(id, rec.definition);

  const list = [...map.values()];
  for (const a of list) {
    let lastResult = null;
    let toolCount = 0;
    let hasText = false;
    for (const e of a.events) {
      if (e.type === "tool_call") toolCount++;
      else if (e.type === "tool_result") lastResult = e;
      else if (e.type === "assistant_message") hasText = true;
    }
    a.toolCount = toolCount;
    a.done = a.closed || (store.agents.get(a.id)?.done ?? hasText);
    a.status = lastResult?.is_error ? "error" : a.done ? "done" : "running";
    stamp(a);
  }
  return list;
}

function parseTs(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d) ? null : d.getTime();
}

// Live events carry no ts, replayed ones do; fall back to first-seen wall time.
function stamp(a) {
  let t = times.get(a.id);
  if (!t) { t = { start: parseTs(a.ts) ?? Date.now(), end: null }; times.set(a.id, t); }
  if (a.done) {
    const last = a.events[a.events.length - 1];
    if (t.end == null) t.end = parseTs(a.closedTs) ?? parseTs(last?.ts) ?? Date.now();
  } else {
    t.end = null;
  }
}

function fmtDur(ms) {
  if (ms == null || ms < 0) return "";
  if (ms < 60000) return fmtMs(Math.round(ms));
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function durText(a) {
  const t = times.get(a.id);
  if (!t) return "";
  return fmtDur((t.end ?? Date.now()) - t.start);
}

// ---- rendering ----

function render() {
  if (!root) return;
  const list = root.querySelector(".pa-list");
  const keep = list.scrollTop;
  const agents = buildAgents();

  const running = agents.filter((a) => !a.done).length;
  root.querySelector(".pa-summary").innerHTML = agents.length
    ? `<span class="pa-sum-n">${agents.length}</span> total
       <span class="pa-sep">·</span>
       <span class="pa-sum-run">${running}</span> running
       <span class="pa-sep">·</span>
       <span class="pa-sum-done">${agents.length - running}</span> done`
    : `<span class="pa-sum-idle">no subagents yet</span>`;

  list.innerHTML = "";
  if (!agents.length) {
    list.appendChild(el(`<div class="pa-empty">
      <div class="pa-empty-mark">⛓</div>
      <div class="pa-empty-title">No subagents running</div>
      <div class="pa-empty-hint">The <code>agent</code> tool spawns a subagent with its own
        context and tool loop. Ask for work that fans out — a broad search, a parallel
        refactor — and each one shows up here with its live transcript.</div>
    </div>`));
    setTicking(false);
    return;
  }
  for (const a of agents) list.appendChild(cardNode(a));
  list.scrollTop = keep;
  setTicking(agents.some((a) => !a.done));
}

function cardNode(a) {
  const open = openIds.has(a.id);
  const card = el(`<div class="pa-card ${open ? "open" : ""}" data-agent="${esc(a.id)}">
    <div class="pa-head" role="button" tabindex="0" aria-expanded="${open}">
      <span class="pa-caret">▸</span>
      <span class="pa-dot pa-${esc(a.status)} ${a.done ? "" : "pa-live"}"></span>
      <span class="pa-id">${esc(a.id)}</span>
      <span class="pa-def">${esc(a.definition || "agent")}</span>
      <span class="pa-count">${a.toolCount} ${a.toolCount === 1 ? "call" : "calls"}</span>
      <span class="pa-dur" data-dur="${esc(a.id)}">${esc(durText(a))}</span>
    </div>
    <div class="pa-body"></div></div>`);

  fillBody(card.querySelector(".pa-body"), a);
  const head = card.querySelector(".pa-head");
  const toggle = () => {
    const nowOpen = !card.classList.contains("open");
    card.classList.toggle("open", nowOpen);
    head.setAttribute("aria-expanded", String(nowOpen));
    if (nowOpen) openIds.add(a.id); else openIds.delete(a.id);
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return card;
}

function fillBody(body, a) {
  for (const e of a.events) {
    if (e.type === "tool_call") body.appendChild(toolNode(a.id, e));
    else if (e.type === "tool_result") applyResult(body, a.id, e);
    else if (e.type === "assistant_message") {
      body.appendChild(el(`<div class="pa-text">${esc(e.text || "")}</div>`));
    }
  }
  const live = a.done ? "" : store.agents.get(a.id)?.streamText || "";
  // A just-spawned agent has nothing to show yet; the streaming node alone
  // would render as an empty body, so say what we are waiting for.
  if (!a.events.length && !live) {
    body.appendChild(el(`<div class="pa-waiting">waiting for the first step…</div>`));
  }
  if (!a.done) {
    body.appendChild(
      el(`<div class="pa-text pa-streaming" data-live="${esc(a.id)}">${esc(live)}</div>`));
  }
}

// One-line argument preview, mirroring the chat view's tool summaries.
function argSummary(name, argsRaw) {
  let a;
  try { a = JSON.parse(argsRaw || "{}"); } catch { return oneLine(argsRaw, 100); }
  if (!a || typeof a !== "object") return oneLine(argsRaw, 100);
  if (name === "bash") return oneLine(a.command, 100);
  if (name === "read" || name === "write" || name === "edit")
    return oneLine(a.file_path || a.path, 100);
  if (name === "grep") return oneLine(`${a.pattern ?? ""}  ${a.path || ""}`, 100);
  if (name === "glob") return oneLine(a.pattern, 100);
  if (name === "agent") return oneLine(a.definition || a.prompt, 100);
  return oneLine(
    Object.entries(a).map(([k, v]) => `${k}: ${oneLine(String(v), 32)}`).join(", "), 100);
}

function pretty(argsRaw) {
  try { return JSON.stringify(JSON.parse(argsRaw || "{}"), null, 2); } catch { return argsRaw ?? ""; }
}

function toolNode(agentId, ev) {
  const key = `${agentId}::${ev.id}`;
  const open = openCalls.has(key);
  const node = el(`<div class="pa-tool ${open ? "open" : ""}" data-call="${esc(ev.id)}">
    <div class="pa-tool-head" role="button" tabindex="0" aria-expanded="${open}">
      <span class="pa-dot pa-running pa-live"></span>
      <span class="pa-tool-name">${esc(ev.name)}</span>
      <span class="pa-tool-args">${esc(argSummary(ev.name, ev.arguments))}</span>
      <span class="pa-tool-ms"></span>
    </div>
    <div class="pa-tool-body">
      <div class="pa-lbl">Arguments</div><pre>${esc(pretty(ev.arguments))}</pre>
      <div class="pa-result"></div>
    </div></div>`);
  const head = node.querySelector(".pa-tool-head");
  const toggle = () => {
    const nowOpen = !node.classList.contains("open");
    node.classList.toggle("open", nowOpen);
    head.setAttribute("aria-expanded", String(nowOpen));
    if (nowOpen) openCalls.add(key); else openCalls.delete(key);
  };
  head.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); toggle(); }
  });
  return node;
}

function applyResult(body, agentId, ev) {
  const node = body.querySelector(`.pa-tool[data-call="${CSS.escape(String(ev.id ?? ""))}"]`);
  if (!node) return;
  const dot = node.querySelector(".pa-dot");
  dot.className = `pa-dot ${ev.is_error ? "pa-error" : "pa-done"}`;
  if (ev.ms != null) node.querySelector(".pa-tool-ms").textContent = fmtMs(ev.ms);
  node.querySelector(".pa-result").innerHTML =
    `<div class="pa-lbl">${ev.is_error ? "Error" : "Result"}</div><pre>${esc(ev.content)}</pre>`;
  const key = `${agentId}::${ev.id}`;
  if (ev.is_error && !autoOpened.has(key)) {   // surface failures once, then obey the user
    autoOpened.add(key);
    openCalls.add(key);
    node.classList.add("open");
    node.querySelector(".pa-tool-head").setAttribute("aria-expanded", "true");
  }
}

// Live text delta: patch the one node instead of rebuilding the roster.
function renderAgentStream(agentId) {
  if (!root) return;
  const rec = store.agents.get(agentId);
  if (!rec || rec.done) return;
  const node = root.querySelector(`.pa-text[data-live="${CSS.escape(String(agentId))}"]`);
  if (!node) return schedule();
  node.textContent = rec.streamText;
  if (rec.streamText) node.parentElement?.querySelector(".pa-waiting")?.remove();
}

// ---- duration ticker ----

function setTicking(on) {
  if (on && !ticker) ticker = setInterval(tick, 1000);
  else if (!on && ticker) { clearInterval(ticker); ticker = null; }
}

function tick() {
  if (!root) return;
  let running = false;
  for (const node of root.querySelectorAll(".pa-dur")) {
    const t = times.get(node.dataset.dur);
    if (!t) continue;
    if (t.end == null) { running = true; node.textContent = fmtDur(Date.now() - t.start); }
  }
  if (!running) setTicking(false);
}
