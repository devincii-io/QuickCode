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

import { inspectLink, wireInspect } from "../inspect.js";
import { store, subscribe } from "../store.js";
import { el, esc, fmtMs, oneLine } from "../util.js";

let root = null;
const openIds = new Set();     // agent_id -> card expanded
const openCalls = new Set();   // "agent_id::call_id" -> tool card expanded
const autoOpened = new Set();  // error cards already auto-expanded once
const times = new Map();       // agent_id -> {start, end}
const shownCap = new Map();    // agent_id -> how many of its events are rendered
const scrollTops = new Map();  // agent_id -> body scrollTop, kept across rebuilds
const follow = new Map();      // agent_id -> stick this transcript to its newest line
const index = new Map();       // agent_id -> agent, for the lazy-fill observer
let ticker = null;
let frame = 0;
let io = null;

// Stacked reads better for one agent at a time; columns are the point when
// several run in parallel, and the grid is what a fan-out of dozens needs —
// it wraps into rows instead of running off the right-hand edge.
const LAYOUT_KEY = "qc-agents-layout";
let layout = readLayout();

// Roster filters. A fan-out of fifty is only readable if you can ask it a
// question: who is still running, who failed, which one was searching CSS.
let statusFilter = "all";      // all | running | done | error
let query = "";
let soloId = null;             // one agent, full panel

// A long-running agent can accumulate hundreds of calls. Rendering all of
// them turns the panel into a scroll of noise and costs real frame time, so
// each card shows its most recent slice and offers the rest on request.
const EVENT_WINDOW = 60;

// Past this many agents, building every transcript up front costs more frame
// time than it can possibly be worth: bodies are then filled as they scroll
// into view.
const LAZY_AT = 12;

// Past this many, the cards themselves tighten up so more of the fleet fits
// on one screen.
const DENSE_AT = 18;

function readLayout() {
  try {
    const v = localStorage.getItem(LAYOUT_KEY);
    return v === "columns" || v === "grid" ? v : "stacked";
  } catch { return "stacked"; }
}

function setLayout(next) {
  layout = next;
  try { localStorage.setItem(LAYOUT_KEY, next); } catch { /* private mode */ }
  render();
}

export const panel = {
  id: "agents",
  title: "Agents",
  icon: "⛓",
  init(container) {
    root = container;
    root.classList.add("panel-agents");
    root.innerHTML = `<div class="pa-bar">
        <button type="button" class="pa-back" title="Back to all agents">← all</button>
        <div class="pa-summary"></div>
        <input type="search" class="pa-search" placeholder="filter agents…"
               aria-label="Filter agents" spellcheck="false">
        <div class="pa-filters" role="group" aria-label="Status filter">
          <button type="button" class="pa-filt" data-status="all">all</button>
          <button type="button" class="pa-filt" data-status="running">running</button>
          <button type="button" class="pa-filt" data-status="done">done</button>
          <button type="button" class="pa-filt" data-status="error">errors</button>
        </div>
        <div class="pa-layout" role="group" aria-label="Layout">
          <button type="button" class="pa-lay" data-layout="stacked" title="Stacked">▤</button>
          <button type="button" class="pa-lay" data-layout="columns" title="Side by side">▥</button>
          <button type="button" class="pa-lay" data-layout="grid" title="Grid">▦</button>
        </div>
      </div><div class="pa-list"></div>`;
    root.querySelectorAll(".pa-lay").forEach((b) =>
      b.addEventListener("click", () => setLayout(b.dataset.layout)));
    root.querySelectorAll(".pa-filt").forEach((b) =>
      b.addEventListener("click", () => { statusFilter = b.dataset.status; render(); }));
    root.querySelector(".pa-back").addEventListener("click", () => { soloId = null; render(); });

    const search = root.querySelector(".pa-search");
    search.addEventListener("input", () => { query = search.value.trim().toLowerCase(); render(); });
    search.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { search.value = ""; query = ""; render(); }
    });
    // Escape leaves the solo view from anywhere in the panel.
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && soloId && e.target !== search) { soloId = null; render(); }
    });

    subscribe(onStoreChange);
    render();
  },
};

function onStoreChange(kind, ev) {
  if (kind === "reset") {
    openIds.clear(); openCalls.clear(); autoOpened.clear(); times.clear();
    shownCap.clear(); scrollTops.clear(); follow.clear();
    soloId = null;
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
      get(ev.agent_id).events.push({ ...ev.ev, ts: ev.ts, seq: ev.seq });
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
    let lastLine = "";
    for (const e of a.events) {
      if (e.type === "tool_call") {
        toolCount++;
        lastLine = `${e.name} ${argSummary(e.name, e.arguments)}`;
      } else if (e.type === "tool_result") {
        lastResult = e;
      } else if (e.type === "assistant_message") {
        hasText = true;
        if (e.text) lastLine = e.text;
      }
    }
    a.toolCount = toolCount;
    a.done = a.closed || (store.agents.get(a.id)?.done ?? hasText);
    a.status = lastResult?.is_error ? "error" : a.done ? "done" : "running";
    // What this agent is doing right now, for the collapsed card. A roster of
    // fifty identical headers says nothing; one live line each says a lot.
    a.lastLine = oneLine(store.agents.get(a.id)?.streamText || lastLine, 120);
    stamp(a);
  }
  return list;
}

function matches(a) {
  if (statusFilter !== "all" && a.status !== statusFilter) return false;
  if (!query) return true;
  return `${a.id} ${a.definition} ${a.lastLine}`.toLowerCase().includes(query);
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
  rememberScrolls(list);
  const listWasPinned = atBottom(list);
  const keep = list.scrollTop;

  const all = buildAgents();
  index.clear();
  for (const a of all) index.set(a.id, a);
  if (soloId && !index.has(soloId)) soloId = null;
  const shown = soloId ? [index.get(soloId)] : all.filter(matches);

  const mode = soloId ? "solo" : layout;
  list.classList.toggle("pa-cols", mode === "columns");
  list.classList.toggle("pa-grid", mode === "grid");
  list.classList.toggle("pa-solo-view", mode === "solo");
  list.classList.toggle("pa-dense", !soloId && all.length >= DENSE_AT);
  root.classList.toggle("pa-soloing", !!soloId);
  root.querySelectorAll(".pa-lay").forEach((b) =>
    b.classList.toggle("on", b.dataset.layout === layout));
  root.querySelectorAll(".pa-filt").forEach((b) =>
    b.classList.toggle("on", b.dataset.status === statusFilter));

  renderSummary(all, shown);

  // Rebuilding the list orphans every observed card, so start the watch over.
  observer(list).disconnect();
  list.innerHTML = "";
  if (!all.length) {
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
  if (!shown.length) {
    list.appendChild(el(`<div class="pa-nomatch">No agent matches this filter.</div>`));
    setTicking(all.some((a) => !a.done));
    return;
  }

  const lazy = !soloId && shown.length > LAZY_AT;
  for (const a of shown) {
    const card = cardNode(a, lazy);
    list.appendChild(card);
    if (lazy) observer(list).observe(card);
  }
  // A card filled during the loop above could not measure itself yet, so the
  // follow/restore pass runs once the whole list is in the document.
  list.querySelectorAll(".pa-body[data-filled]").forEach(applyScroll);
  list.scrollTop = listWasPinned && mode === "stacked" ? list.scrollHeight : keep;
  setTicking(all.some((a) => !a.done));
}

function renderSummary(all, shown) {
  const bar = root.querySelector(".pa-summary");
  if (soloId) {
    const a = index.get(soloId);
    bar.innerHTML = `<span class="pa-sum-n">${esc(a.id)}</span>
      <span class="pa-sep">·</span> ${esc(a.definition || "agent")}`;
    return;
  }
  if (!all.length) { bar.innerHTML = `<span class="pa-sum-idle">no subagents yet</span>`; return; }
  const running = all.filter((a) => !a.done).length;
  const failed = all.filter((a) => a.status === "error").length;
  const filtered = shown.length !== all.length
    ? ` <span class="pa-sep">·</span> <span class="pa-sum-filt">${shown.length} shown</span>`
    : "";
  bar.innerHTML = `<span class="pa-sum-n">${all.length}</span> total
     <span class="pa-sep">·</span>
     <span class="pa-sum-run">${running}</span> running
     <span class="pa-sep">·</span>
     <span class="pa-sum-done">${all.length - running - failed}</span> done${
       failed ? ` <span class="pa-sep">·</span>
         <span class="pa-sum-err">${failed}</span> failed` : ""}${filtered}`;
}

// Fill a transcript only once it can actually be seen. With fifty agents the
// difference is a panel that opens instantly and one that stalls for a second.
function observer(list) {
  if (io) return io;
  io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const card = e.target;
      const body = card.querySelector(".pa-body");
      const a = index.get(card.dataset.agent);
      if (!body || body.dataset.filled || !a) continue;
      if (!card.classList.contains("open")) continue;
      fillBody(body, a);
      applyScroll(body);
    }
  }, { root: list, rootMargin: "300px" });
  return io;
}

// Each agent's transcript scrolls on its own, and a rebuild must not throw
// the reader back to the top of the one they were reading — nor unstick one
// they had left pinned to its newest line.
function rememberScrolls(list) {
  list.querySelectorAll(".pa-body").forEach((body) => {
    const id = body.dataset.body;
    if (id && body.scrollTop) scrollTops.set(id, body.scrollTop);
  });
}

function atBottom(node, slack = 24) {
  return node.scrollHeight - node.scrollTop - node.clientHeight <= slack;
}

// Auto-scroll, but only while the reader has not scrolled away: a transcript
// that yanks itself downward under someone reading it is worse than one that
// never moves.
function applyScroll(body) {
  const id = body.dataset.body;
  if (follow.get(id) === false) body.scrollTop = scrollTops.get(id) || 0;
  else body.scrollTop = body.scrollHeight;
  markPinned(body);
}

function markPinned(body) {
  const pinned = atBottom(body);
  follow.set(body.dataset.body, pinned);
  body.closest(".pa-card")?.classList.toggle("unpinned", !pinned);
}

function cardNode(a, lazy) {
  // In columns, grid, and solo every agent is open: a column of collapsed
  // headers would be a worse version of the stacked view, not a different one.
  const open = layout === "stacked" && !soloId ? openIds.has(a.id) : true;
  const card = el(`<div class="pa-card ${open ? "open" : ""}" data-agent="${esc(a.id)}">
    <div class="pa-head" role="button" tabindex="0" aria-expanded="${open}">
      <span class="pa-caret">▸</span>
      <span class="pa-dot pa-${esc(a.status)} ${a.done ? "" : "pa-live"}"></span>
      <span class="pa-id">${esc(a.id)}</span>
      <span class="pa-def">${esc(a.definition || "agent")}</span>
      <span class="pa-count">${a.toolCount} ${a.toolCount === 1 ? "call" : "calls"}</span>
      <span class="pa-dur" data-dur="${esc(a.id)}">${esc(durText(a))}</span>
      <button type="button" class="pa-solo"
        title="${soloId ? "Back to all agents" : "Show only this agent"}"
        aria-label="${soloId ? "Back to all agents" : "Show only this agent"}"
        >${soloId ? "exit" : "solo"}</button>
    </div>
    <div class="pa-last">${esc(a.lastLine || "")}</div>
    <div class="pa-body" data-body="${esc(a.id)}"></div>
    <button type="button" class="pa-jump" title="Jump to the newest line">↓ latest</button>
    </div>`);

  const body = card.querySelector(".pa-body");
  if (open && !lazy) fillBody(body, a);
  body.addEventListener("scroll", () => {
    scrollTops.set(a.id, body.scrollTop);
    markPinned(body);
  });
  card.querySelector(".pa-jump").addEventListener("click", (e) => {
    e.stopPropagation();
    follow.set(a.id, true);
    body.scrollTop = body.scrollHeight;
    markPinned(body);
  });
  card.querySelector(".pa-solo").addEventListener("click", (e) => {
    e.stopPropagation();
    soloId = soloId ? null : a.id;
    render();
  });

  const head = card.querySelector(".pa-head");
  const toggle = () => {
    const nowOpen = !card.classList.contains("open");
    card.classList.toggle("open", nowOpen);
    head.setAttribute("aria-expanded", String(nowOpen));
    if (nowOpen) {
      openIds.add(a.id);
      if (!body.dataset.filled) { fillBody(body, index.get(a.id) || a); applyScroll(body); }
    } else {
      openIds.delete(a.id);
    }
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return card;
}

function fillBody(body, a) {
  body.dataset.filled = "1";
  const cap = shownCap.get(a.id) || EVENT_WINDOW;
  const hidden = Math.max(0, a.events.length - cap);
  if (hidden) {
    const more = el(`<button type="button" class="pa-more">show ${hidden} earlier ${
      hidden === 1 ? "step" : "steps"}</button>`);
    // Raising the cap rather than clearing it keeps the next click bounded too.
    more.addEventListener("click", () => {
      shownCap.set(a.id, cap + EVENT_WINDOW * 4);
      render();
    });
    body.appendChild(more);
  }
  // A tool_result must not be dropped while its tool_call survives, so the
  // window is applied to the tail and results are matched inside it.
  for (const e of a.events.slice(hidden)) {
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
      ${ev.seq != null ? `<div class="pa-lbl pa-trace">${inspectLink(ev.seq)}</div>` : ""}
    </div></div>`);
  wireInspect(node);
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
  const card = root.querySelector(`.pa-card[data-agent="${CSS.escape(String(agentId))}"]`);
  if (!card) return schedule();
  const last = card.querySelector(".pa-last");
  if (last) last.textContent = oneLine(rec.streamText, 120);
  const body = card.querySelector(".pa-body");
  // Not filled yet (collapsed, or scrolled out of view): nothing to paint, and
  // forcing a rebuild for an invisible agent is exactly what we are avoiding.
  if (!body?.dataset.filled) return;
  const node = body.querySelector(`.pa-text[data-live="${CSS.escape(String(agentId))}"]`);
  if (!node) return schedule();
  node.textContent = rec.streamText;
  if (rec.streamText) body.querySelector(".pa-waiting")?.remove();
  if (follow.get(agentId) !== false) body.scrollTop = body.scrollHeight;
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
