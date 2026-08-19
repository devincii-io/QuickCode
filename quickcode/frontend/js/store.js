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

// A round that ends in tool calls is a pause, not an ending: the agent runs the
// tools and asks the model again. Everything else — stop, length, error, or a
// provider that said nothing — ends the turn.
export function midTurn(finishReason) {
  return finishReason === "tool_calls" || finishReason === "function_call";
}

// The three states the loop stops in. Anything still marked in-flight when one
// of these arrives never will finish: an interrupted tool call is answered in
// the message history and emits no `tool_result` at all (core/loop.py), so the
// maps below would keep it for ever and the activity line would name a tool
// that is not running on the *next* turn.
const TERMINAL_STATUS = new Set(["idle", "interrupted", "error"]);

function endOfTurn(state) {
  store.runningTools.clear();
  store.pendingCalls.clear();
  // An interrupt takes the whole tree down — Conversation.interrupt() cancels
  // the agent *and* every background job, and a blocking child dies with the
  // tool call awaiting it. Only detached jobs get an `agent_done` to say so,
  // so without this the rest of the fan-out sits in the roster as "running"
  // for ever, and their cards keep claiming they are still thinking.
  if (state !== "interrupted") return;
  for (const a of store.agents.values()) {
    if (a.done) continue;
    a.done = true;
    a.streamText = "";
    a.status = "interrupted";
  }
}

// Replay carries no status events, so an agent whose terminal record never got
// written — an interrupt kills a blocking child before it can produce one —
// pulsed "running" again on every reconnect. `busy` is the server's own word
// for whether anything is still in flight, and it arrives on the state event
// just before the replay. Presumed, not concluded: a detached job really can
// outlive the turn that spawned it, so one live event revives the row.
function presumeSettled() {
  if (!store.state || store.state.busy) return;
  for (const a of store.agents.values()) {
    if (a.done) continue;
    a.done = true;
    a.presumed = true;
  }
}

// A blocking subagent's report coming back to the parent is proof the child is
// over — and for one that died without writing a terminal message of its own
// (its turn raised, or its last round produced no text) it is the only proof
// in the log, which is why such a row used to sit in the roster as "running"
// for the rest of the session. `<subagent id="…">` is the wrapper the agent
// tool puts around every report, and `[did not finish]` is how the runner tags
// one that failed (tools/agent.py, subagents/runner.py). Presumed, not
// concluded: a later event from that agent takes it straight back.
// The wrapper the agent/send_message tools put around a report. `status` is the
// authority (the runner decided it); the `[did not finish]` marker is the
// fallback for logs written before the attribute existed — and note it can sit
// *after* the sanitizer's own prefix line, which is why matching it needs the
// optional hop rather than `\s*`.
const REPORT_TAG =
  /<subagent id="([^"]+)"(?:\s+status="([^"]*)")?>\s*(?:\[quickcode:[^\]]*\]\s*)?(\[did not finish\])?/g;

function settleFromReport(content) {
  if (typeof content !== "string" || !content.includes("<subagent id=")) return;
  for (const [, id, status, marker] of content.matchAll(REPORT_TAG)) {
    const a = store.agents.get(id);
    if (!a || a.done) continue;
    a.done = true;
    // A status attribute is a fact from the runner, not an inference from the
    // report's text, so a report that carries one is not "presumed" anything.
    a.presumed = !status;
    a.streamText = "";
    if (marker || (status && status !== "done")) a.status = status || "error";
  }
}

// It spoke: it was alive after all.
function revive(a) {
  if (!a?.presumed) return;
  a.presumed = false;
  a.done = false;
  a.status = undefined;
}

function agentRecord(id, definition) {
  let a = store.agents.get(id);
  // A spawn always precedes its events in the log, but an out-of-order or
  // truncated stream must not leave an agent the store cannot see at all.
  if (!a) { a = { definition: definition || "", streamText: "", done: false }; store.agents.set(id, a); }
  else if (definition && !a.definition) a.definition = definition;
  return a;
}

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
  if (t === "replay_done") {
    store.replaying = false;
    presumeSettled();
    notify("replay_done");
    return;
  }

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
      settleFromReport(ev.content);
    } else if (t === "agent_spawned") {
      const a = agentRecord(ev.agent_id, ev.definition);
      a.definition = ev.definition;
      revive(a);
    } else if (t === "agent_event") {
      const a = agentRecord(ev.agent_id);
      revive(a);
      if (ev.ev?.type === "assistant_message") {
        a.streamText = "";
        // The provider emits TurnDone once per *round*, so the recorder
        // synthesises an assistant_message every time a round produced text —
        // including the rounds that end in tool calls and carry on working.
        // Treating any of them as terminal marked a busy subagent "done", hid
        // it behind the roster's running filter, and stopped its live text from
        // rendering at all. `finish_reason` is the difference.
        if (!midTurn(ev.ev.finish_reason)) a.done = true;
      }
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
      store.agentStatus = ev.state;
      if (TERMINAL_STATUS.has(ev.state)) endOfTurn(ev.state);
      notify("status", ev); break;
    case "tasks":
      notify("tasks", ev); break;
    case "queued_message":
      notify("queued", ev); break;
    case "agent_event": {
      const a = store.agents.get(ev.agent_id);
      revive(a);
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

// A subagent's result is wrapped in an `agent_event`, so matching on the
// top-level type alone found nothing for every call a subagent made — the
// inspector then reported "not applicable" for a result that is in the log.
// `agentId` scopes the search to the agent that made the call, since ids are
// only unique within one agent's stream.
export function toolResultFor(callId, agentId = null) {
  for (const e of store.events) {
    const inner = e.type === "agent_event" ? e.ev : e;
    if (!inner || inner.type !== "tool_result" || inner.id !== callId) continue;
    if ((e.agent_id ?? null) !== (agentId ?? null)) continue;
    return inner;
  }
  return undefined;
}

export function eventBySeq(seq) {
  return store.events.find((e) => e.seq === seq);
}
