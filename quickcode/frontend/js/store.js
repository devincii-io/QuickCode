// Central event store: the client-side mirror of the append-only session log.
// Both the chat renderer and the trajectory view subscribe here; live deltas
// update streaming buffers, logged events append to `events` (deduped by seq).

const subs = new Set();

export const store = {
  bootstrap: null,          // /api/bootstrap payload
  projectId: null,          // project the shell is attached to
  convId: null,
  state: null,              // last "state" event
  events: [],               // logged events (have seq)
  seenSeq: new Set(),
  // live streaming buffers (main agent)
  streamText: "",
  streamReasoning: "",
  pendingCalls: new Map(),  // id -> {name, argsBuf} while a call streams
  runningTools: new Map(),  // id -> {name, arguments} awaiting result
  agents: new Map(),        // agent_id -> {definition, streamText, done}
  agentStatus: "idle",
  connection: "connecting", // connecting | open | closed
  replaying: false,
  // Measured session metrics for the status bar. Counts come from the event
  // log (so they survive replay); the timings can only be measured live, and
  // stay null until this client has actually watched a turn.
  metrics: emptyMetrics(),
};

function emptyMetrics() {
  return {
    turns: 0,          // user messages
    steps: 0,          // tool calls
    toolMs: 0,         // summed tool wall-clock
    llmMs: 0,          // summed time from request to round end (live only)
    ttftMs: null,      // time to first token of the last turn (live only)
    tps: null,         // output tokens/sec over the last turn (live only)
    _turnStart: 0,
    _firstToken: 0,
    _outputAtTurnStart: 0,
  };
}

export function subscribe(fn) { subs.add(fn); return () => subs.delete(fn); }

function notify(kind, ev) { for (const fn of subs) fn(kind, ev); }

export function resetConversation() {
  store.state = null;
  store.events = [];
  store.seenSeq = new Set();
  store.streamText = "";
  store.streamReasoning = "";
  store.pendingCalls = new Map();
  store.runningTools = new Map();
  store.agents = new Map();
  store.agentStatus = "idle";
  store.metrics = emptyMetrics();
  notify("reset");
}

export function setConnection(state) {
  store.connection = state;
  notify("connection");
}

// Feed one server event (replayed or live) into the store.
export function ingest(ev) {
  const t = ev.type;

  if (t === "state") {
    store.state = ev;
    notify("state", ev);
    return;
  }
  if (t === "replay_start") { store.replaying = true; notify("replay_start"); return; }
  if (t === "replay_done") { store.replaying = false; notify("replay_done"); return; }

  // Logged events carry seq; dedupe (replay can overlap the live stream).
  if (ev.seq != null) {
    if (store.seenSeq.has(ev.seq)) return;
    store.seenSeq.add(ev.seq);
    store.events.push(ev);
    countMetric(ev);
    // An assembled record supersedes live buffers.
    if (t === "assistant_message") {
      store.streamText = "";
      store.streamReasoning = "";
    } else if (t === "tool_call") {
      store.pendingCalls.delete(ev.id);
      store.runningTools.set(ev.id, ev);
    } else if (t === "tool_result") {
      store.runningTools.delete(ev.id);
    } else if (t === "agent_spawned") {
      store.agents.set(ev.agent_id, { definition: ev.definition, streamText: "", done: false });
    } else if (t === "agent_event") {
      const a = store.agents.get(ev.agent_id);
      if (a && ev.ev?.type === "assistant_message") { a.streamText = ""; a.done = true; }
    } else if (t === "agent_done") {
      // A detached job's closing bracket. A blocking subagent goes terminal on
      // its assistant_message above; a background one finishes at a moment
      // nothing else in the stream marks, so this is the only signal the
      // roster gets that the row is over.
      const a = store.agents.get(ev.agent_id);
      if (a) { a.streamText = ""; a.done = true; a.status = ev.status; }
    }
    notify("event", ev);
    return;
  }

  // Live-only events.
  switch (t) {
    case "text_delta":
      markFirstToken();
      store.streamText += ev.text; notify("stream"); break;
    case "reasoning_delta":
      store.streamReasoning += ev.text; notify("stream"); break;
    case "tool_call_start":
      store.pendingCalls.set(ev.id, { name: ev.name, argsBuf: "" }); notify("stream"); break;
    case "tool_call_delta": {
      const c = store.pendingCalls.get(ev.id);
      if (c) c.argsBuf += ev.arguments;
      break;
    }
    case "status":
      store.agentStatus = ev.state; notify("status", ev); break;
    case "tasks":
      notify("tasks", ev); break;
    case "queued_message":
      notify("queued", ev); break;
    case "agent_event": {
      const a = store.agents.get(ev.agent_id);
      if (a && ev.ev?.type === "text_delta") a.streamText += ev.ev.text;
      notify("agent_stream", ev); break;
    }
    case "round_done":
      endTurnTiming();
      notify("stream"); break;
    default:
      notify("misc", ev);
  }
}

// ---- metrics ----

// Counts are derived from logged events, so a replayed session reports the
// same numbers a live one did. Timings cannot work that way -- the log has no
// record of how long the model took to start talking -- so they are measured
// only while this client is watching, and read as "–" until then.

function countMetric(ev) {
  const m = store.metrics;
  if (ev.type === "user_message") {
    m.turns += 1;
    if (!store.replaying) {
      m._turnStart = performance.now();
      m._firstToken = 0;
      m._outputAtTurnStart = mainOutput();
    }
  } else if (ev.type === "tool_call") {
    m.steps += 1;
  } else if (ev.type === "tool_result") {
    m.toolMs += ev.ms || 0;
  }
}

// Output tokens this client actually watched arrive. Subagents run their own
// loops off-screen, so counting their output here would report a tokens/second
// the streaming model never ran at — a fan-out of four would read as 4× fast.
function mainOutput() {
  const l = store.state?.ledger;
  return (l?.output_tokens || 0) - (l?.subagent_output_tokens || 0);
}

function markFirstToken() {
  const m = store.metrics;
  if (m._turnStart && !m._firstToken) {
    m._firstToken = performance.now();
    m.ttftMs = m._firstToken - m._turnStart;
  }
}

function endTurnTiming() {
  const m = store.metrics;
  if (!m._turnStart) return;
  const now = performance.now();
  m.llmMs += now - m._turnStart;
  // Tokens per second is measured from the first token, not from the request:
  // including the wait to first token would report a rate the model never ran at.
  const produced = mainOutput() - m._outputAtTurnStart;
  const seconds = (now - (m._firstToken || m._turnStart)) / 1000;
  if (produced > 0 && seconds > 0.05) m.tps = produced / seconds;
  m._turnStart = 0;
}

// ---- derived helpers ----

export function toolResultFor(callId) {
  return store.events.find((e) => e.type === "tool_result" && e.id === callId);
}

export function eventBySeq(seq) {
  return store.events.find((e) => e.seq === seq);
}
