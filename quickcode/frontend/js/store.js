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
};

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
    }
    notify("event", ev);
    return;
  }

  // Live-only events.
  switch (t) {
    case "text_delta":
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
      notify("stream"); break;
    default:
      notify("misc", ev);
  }
}

// ---- derived helpers ----

export function toolResultFor(callId) {
  return store.events.find((e) => e.type === "tool_result" && e.id === callId);
}

export function eventBySeq(seq) {
  return store.events.find((e) => e.seq === seq);
}
