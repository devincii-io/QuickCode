// WebSocket connection to one conversation. The token rides as a subprotocol
// (browsers can't set WS headers). Reconnects with backoff; every reconnect
// replays the event log and the store dedupes by seq.
//
// Exactly one socket may reach the store at a time. Sequence numbers restart
// at 1 in every conversation, so a single late frame from a superseded socket
// would poison `seenSeq` and make the next replay dedupe itself away — an
// empty transcript. `generation` is the guard: every connect/disconnect bumps
// it, and a socket whose generation is stale is deaf and mute.

import { authToken } from "./api.js";
import { ingest, resetConversation, setConnection, store } from "./store.js";

let ws = null;
let convId = null;
let projectId = null;
let backoff = 500;
let generation = 0;
let retryTimer = null;

// Attach to one conversation of one project. Switching either tears the old
// socket down and resets the store, so no state leaks across the switch.
export function connect(pid, id) {
  teardown();
  projectId = pid;
  convId = id;
  store.convId = id;
  store.projectId = pid;
  backoff = 500;
  resetConversation();
  open();
}

export function disconnect() {
  teardown();
  convId = null;
  // Leaving a conversation empties the mirror: the store must only ever hold
  // the conversation currently attached.
  resetConversation();
}

// Retire the current socket *and* its handlers, and cancel any pending
// reconnect. Detaching the handlers is what makes the retirement final —
// close() alone leaves a socket that can still deliver queued frames.
function teardown() {
  generation++;
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch { /* already closing */ }
    ws = null;
  }
}

function open() {
  const mine = generation;
  setConnection("connecting");
  const proto = authToken() ? ["qcauth." + authToken()] : undefined;
  const sock = new WebSocket(
    `ws://${location.host}/ws/projects/${encodeURIComponent(projectId)}` +
    `/conversation/${encodeURIComponent(convId)}`,
    proto,
  );
  ws = sock;

  sock.onopen = () => {
    if (mine !== generation) return;
    backoff = 500;
    setConnection("open");
  };
  sock.onmessage = (m) => {
    if (mine !== generation) return;
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    ingest(ev);
  };
  sock.onclose = () => {
    if (mine !== generation) return;   // superseded: someone else owns the store
    setConnection("closed");
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (mine !== generation || !convId) return;
      resetConversation();
      open();
    }, backoff);
    backoff = Math.min(backoff * 2, 8000);
  };
  sock.onerror = () => {};
}

export function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

export const actions = {
  userMessage: (text) => send({ type: "user_message", text }),
  interrupt: () => send({ type: "interrupt" }),
  setMode: (mode) => send({ type: "set_mode", mode }),
  setModel: (model) => send({ type: "set_model", model }),
  compact: () => send({ type: "compact" }),
  permissionDecision: (req_id, allow, persist, deny_message = "") =>
    send({ type: "permission_decision", req_id, allow, persist, deny_message }),
  planDecision: (req_id, approved, mode_after = null, feedback = "") =>
    send({ type: "plan_decision", req_id, approved, mode_after, feedback }),
};
