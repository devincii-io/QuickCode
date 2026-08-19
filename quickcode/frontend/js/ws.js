// WebSocket connection to one conversation. The token rides as a subprotocol
// (browsers can't set WS headers). Reconnects with backoff; every reconnect
// replays the event log from scratch, and the store — emptied on the new
// socket's first frame, see `open()` — dedupes by seq.
//
// Exactly one socket may reach the store at a time. Sequence numbers restart
// at 1 in every conversation, so a single late frame from a superseded socket
// would poison `seenSeq` and make the next replay dedupe itself away — an
// empty transcript. `generation` is the guard: every connect/disconnect bumps
// it, and a socket whose generation is stale is deaf and mute.
//
// Two further things a socket owes the person watching it, neither automatic:
//
//   - It must never swallow what they typed. `send()` is the one choke point
//     every outbound frame passes through, so a refused frame says so *there*,
//     once, rather than being dropped silently in each of eight callers.
//   - It must be honest about how it is doing. `store.connection` has three
//     words and no memory. `health` below is the rest of the story — how long
//     it has been down, whether it was ever up, and whether trying again could
//     possibly work — which is what main.js turns into a banner.

import { authToken } from "./api.js";
import { ingest, resetConversation, setConnection, store } from "./store.js";
import { toastError } from "./toast.js";

let ws = null;
let convId = null;
let projectId = null;
let backoff = 500;
let generation = 0;
let retryTimer = null;

// Close codes the server uses to say "no", as opposed to "not right now".
// `_attach` (server/app.py) accepts the socket and *then* closes 4404 when the
// conversation does not exist, so a naive retry loop reconnects twice a second
// forever against an answer that will never change. These stop the loop and
// hand the user something to do instead.
const CLOSE_GONE = 4404;      // no such project or conversation
const CLOSE_DENIED = 4403;    // host, origin or token refused

const health = {
  attempts: 0,      // consecutive failed opens since the last live socket
  everOpen: false,  // has this attachment ever carried a frame?
  downSince: 0,     // performance.now() of the close that began this outage
  fatal: "",        // "" | "gone" | "denied" — retrying cannot help
  lastFrame: 0,     // performance.now() of the last frame of any kind
};

// The server sends a `heartbeat` frame every 15 s of quiet (server/app.py
// HEARTBEAT_S). Missing several in a row means the socket is a zombie: after a
// sleep/wake it can sit in OPEN with nothing behind it, where `send` succeeds
// into the void and no frame ever arrives to prove otherwise. Closing it turns
// an invisible dead connection into the ordinary reconnect this file already
// knows how to do. The window is generous — three missed beats — because a
// browser throttles timers in a background tab and a false positive costs a
// pointless replay.
const SILENCE_MS = 50_000;
let watchdog = null;

function watch() {
  if (watchdog) return;
  watchdog = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !health.lastFrame) return;
    if (performance.now() - health.lastFrame < SILENCE_MS) return;
    health.lastFrame = 0;   // one close per silence, not one per tick
    try { ws.close(4001, "no heartbeat"); } catch { /* already going */ }
  }, 5000);
}

/** How the connection is doing, for whoever has to say it out loud. */
export function connectionHealth() {
  return { ...health, state: store.connection, attached: !!convId };
}

// Attach to one conversation of one project. Switching either tears the old
// socket down and resets the store, so no state leaks across the switch.
export function connect(pid, id) {
  teardown();
  projectId = pid;
  convId = id;
  store.convId = id;
  store.projectId = pid;
  backoff = 500;
  health.attempts = 0;
  health.everOpen = false;
  health.downSince = 0;
  health.fatal = "";
  health.lastFrame = 0;
  resetConversation();
  open();
  watch();
}

export function disconnect() {
  teardown();
  convId = null;
  health.downSince = 0;
  health.fatal = "";
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
  if (watchdog) { clearInterval(watchdog); watchdog = null; }
  health.lastFrame = 0;
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch { /* already closing */ }
    ws = null;
  }
  // `resetConversation` does not clear this flag, and a socket that dies
  // between replay_start and replay_done would otherwise leave it true for as
  // long as the server stays away — with modals.js suppressing every
  // permission request and trajectory.js dropping every event it is handed.
  store.replaying = false;
}

function open() {
  const mine = generation;
  let mirrored = false;        // has this socket taken ownership of the store?
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
    setConnection("open");
  };
  sock.onmessage = (m) => {
    if (mine !== generation) return;
    // The backoff resets here rather than in onopen, because a socket that is
    // accepted and then closed (4404) has "opened" without ever being useful.
    // Proof of usefulness is a frame.
    health.everOpen = true;
    health.attempts = 0;
    health.downSince = 0;
    health.lastFrame = performance.now();
    backoff = 500;
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    // The server's proof of life on an idle conversation. It is not a store
    // event and must not reset the mirror: it says the socket is alive, not
    // that a new stream has begun.
    if (ev.type === "heartbeat") return;
    // The mirror is emptied here — on this socket's very first frame — rather
    // than before the reconnect that will refill it. Clearing it early wiped
    // the transcript within half a second of a drop and left it blank for the
    // whole outage, which reads as "the session is gone" over a session that
    // is perfectly intact on disk. Doing it on the first frame keeps `seenSeq`
    // empty before any replayed event can arrive (the invariant the reset
    // exists for) while swapping the old transcript for the new one in a
    // single frame instead of deleting it and slowly retyping it.
    //
    // Keyed off "first frame" rather than off `replay_start`, because the
    // server sends the `state` event ahead of that marker: resetting on the
    // marker would null out the state we had just been handed, and with it the
    // `pending` list that is the only thing able to re-raise a permission
    // prompt the drop interrupted.
    //
    // (`connect`/`disconnect` still reset immediately — those are leaving the
    // conversation, not returning to it.)
    if (!mirrored) { mirrored = true; resetConversation(); }
    ingest(ev);
  };
  sock.onclose = (e) => {
    if (mine !== generation) return;   // superseded: someone else owns the store
    store.replaying = false;
    health.attempts += 1;
    if (!health.downSince) health.downSince = performance.now();
    if (e?.code === CLOSE_GONE) health.fatal = "gone";
    else if (e?.code === CLOSE_DENIED) health.fatal = "denied";
    setConnection("closed");
    if (health.fatal) return;          // a "no", not a "not right now"
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (mine !== generation || !convId) return;
      open();
    }, backoff);
    backoff = Math.min(backoff * 2, 8000);
  };
  sock.onerror = () => {};
}

/** Reconnect now instead of waiting out the backoff. The banner's button, and
 *  what the events below call: a wait measured against a dead network is the
 *  wrong wait once there is fresh reason to think it could work. */
export function retryNow() {
  if (!convId) return;
  if (ws && ws.readyState === WebSocket.OPEN) return;
  teardown();                 // bumps the generation, so `open()` owns the store
  backoff = 500;
  health.fatal = "";
  open();                     // the mirror is cleared on the first frame, above
}

// Waking from sleep, coming back online and returning to the window are the
// three moments when a dead socket is most likely to be revivable and most
// likely to be looked at. On loopback they are the difference between a window
// that is live when you glance at it and one that is live eight seconds later.
function retryIfRevivable() {
  if (document.visibilityState === "hidden") return;
  if (!convId || health.fatal) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  retryNow();
}

window.addEventListener("online", retryIfRevivable);
window.addEventListener("focus", retryIfRevivable);
// On `document`, which is where it is dispatched.
document.addEventListener("visibilitychange", retryIfRevivable);

// ---- outbound ----
//
// What each kind of frame failing to leave actually costs the user, in their
// terms. A refusal that says "send failed" teaches nothing; one that says the
// message is still in the box says what to do next.
const REFUSED = {
  user_message: "your message was not sent — it is still in the box",
  interrupt: "the agent was not told to stop",
  set_mode: "the mode did not change",
  set_model: "the model did not change",
  compact: "the conversation was not compacted",
  permission_decision:
    "your answer did not reach the agent — it will ask again once the connection is back",
  plan_decision:
    "your plan review did not reach the agent — it will ask again once the connection is back",
};

// Enter pressed five times at a dead socket is one problem, not five. The
// stack in toast.js caps at four, so without this a burst would also push
// every other notice off the screen.
let lastRefusal = "";
let lastRefusalAt = 0;
const REFUSAL_QUIET_MS = 4000;

function refused(type) {
  const what = REFUSED[type] || "that did not reach the agent";
  const now = performance.now();
  if (what === lastRefusal && now - lastRefusalAt < REFUSAL_QUIET_MS) return;
  lastRefusal = what;
  lastRefusalAt = now;
  toastError(`Not connected to QuickCode — ${what}.`);
}

export function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  refused(obj?.type);
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
