// WebSocket connection to one conversation. The token rides as a subprotocol
// (browsers can't set WS headers). Reconnects with backoff; every reconnect
// replays the event log and the store dedupes by seq.

import { authToken } from "./api.js";
import { ingest, resetConversation, setConnection, store } from "./store.js";

let ws = null;
let convId = null;
let projectId = null;
let backoff = 500;
let closedByUs = false;

// Attach to one conversation of one project. Switching either tears the old
// socket down and resets the store, so no state leaks across the switch.
export function connect(pid, id) {
  closedByUs = false;
  if (ws) { closedByUs = true; ws.close(); }
  projectId = pid;
  convId = id;
  store.convId = id;
  store.projectId = pid;
  resetConversation();
  open();
}

export function disconnect() {
  if (ws) { closedByUs = true; ws.close(); }
  ws = null;
  convId = null;
}

function open() {
  setConnection("connecting");
  const proto = authToken() ? ["qcauth." + authToken()] : undefined;
  ws = new WebSocket(
    `ws://${location.host}/ws/projects/${encodeURIComponent(projectId)}` +
    `/conversation/${encodeURIComponent(convId)}`,
    proto,
  );

  ws.onopen = () => { backoff = 500; setConnection("open"); };
  ws.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    ingest(ev);
  };
  ws.onclose = () => {
    if (closedByUs) { closedByUs = false; return; }
    setConnection("closed");
    setTimeout(() => {
      if (convId) { resetConversation(); open(); }
    }, backoff);
    backoff = Math.min(backoff * 2, 8000);
  };
  ws.onerror = () => {};
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
