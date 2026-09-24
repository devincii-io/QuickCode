// The connection banner (#conn-host).
//
// `store.connection` used to drive one dot in the status bar. A dot is what
// you consult once you are already suspicious; it is not what tells you.
// Meanwhile everything else on screen kept looking live — the status segment
// still said "streaming" because status events had simply stopped arriving,
// and Stop still offered to interrupt a turn it could no longer reach.
//
// The banner is graded, because "the socket closed" covers two very different
// events. A 1013 overflow close is the server *deliberately* making us
// reconnect and replay; it is over in milliseconds and announcing it would be
// crying wolf several times an hour. A server that has stopped is the
// opposite: nothing the user types can ever arrive, and in the native window
// there is no address bar to reload from. So: silence for the first moment, a
// calm line after that, and an unmistakable one — with a button — once it has
// been down long enough to be a real outage.

import { subscribe } from "./store.js";
import { toastOk } from "./toast.js";
import { esc } from "./util.js";
import { connectionHealth, retryNow } from "./ws.js";

const $ = (id) => document.getElementById(id);

const CONN_QUIET_MS = 1200;    // below this, a reconnect is not news
const CONN_LOUD_MS = 12000;    // above this, it is not a blip

let connTimer = null;
let connWasLoud = false;       // an outage the user was actually told about
let onNewSession = () => {};

function connCard({ tone, title, body, action }) {
  return `<div class="conn-card ${tone}" role="${tone === "warn" ? "alert" : "status"}">
    <span class="conn-dot"></span>
    <div class="conn-text">
      <span class="conn-title">${esc(title)}</span>
      <span class="conn-body">${esc(body)}</span>
    </div>
    ${action ? `<button class="btn conn-act" data-act="${action.id}">${
      esc(action.label)}</button>` : ""}
  </div>`;
}

// What to say, given how long it has been down and whether trying again could
// ever work. Split out so the wording is readable as prose in one place.
function connState(h, down) {
  if (h.fatal === "gone") {
    return {
      tone: "warn", title: "This session is no longer on the server",
      body: "QuickCode answered, and the answer was that it does not have this "
        + "conversation any more — the project may have been closed or the "
        + "session file removed. Nothing further will arrive here.",
      action: { id: "new", label: "Start a new session" },
    };
  }
  if (h.fatal === "denied") {
    return {
      tone: "warn", title: "QuickCode refused this connection",
      body: "The token this page is holding is no longer accepted. Open "
        + "QuickCode again from the URL the CLI printed — it carries a fresh one.",
      action: { id: "retry", label: "Try again" },
    };
  }
  if (down >= CONN_LOUD_MS) {
    // A socket that was never once live is a different problem from one that
    // dropped, and the difference is worth saying: the first points at the
    // launch (a stale token, the wrong port), the second at the server.
    return h.everOpen
      ? {
        tone: "warn", title: "QuickCode is not responding",
        body: `Nothing has reached this window for ${Math.round(down / 1000)}s. `
          + "The server may have stopped. Anything you send now will not "
          + "arrive; the window keeps trying in the background.",
        action: { id: "retry", label: "Try again now" },
      }
      : {
        tone: "warn", title: "Could not reach QuickCode",
        body: "This window has never managed to open a connection. The server "
          + "may not be running, or this page may be holding a token it no "
          + "longer accepts — reopening the URL the CLI printed fixes the second.",
        action: { id: "retry", label: "Try again now" },
      };
  }
  return {
    tone: "calm", title: "Reconnecting to QuickCode…",
    body: "The transcript reloads from the server's log as soon as it is back.",
    action: null,
  };
}

function renderConn() {
  const host = $("conn-host");
  if (!host) return;
  if (connTimer) { clearTimeout(connTimer); connTimer = null; }
  const h = connectionHealth();

  // Nothing to have an opinion about: no conversation is attached (Home), the
  // socket is up, or it is the first connect and has not failed yet — a
  // "connecting" that has never closed is just boot.
  if (!h.attached || (h.state === "open" && !h.fatal) || (!h.downSince && !h.fatal)) {
    if (!h.attached) connWasLoud = false;   // no outage to give a receipt for
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }

  const down = performance.now() - h.downSince;
  if (!h.fatal && down < CONN_QUIET_MS) {
    host.classList.add("hidden");
    host.innerHTML = "";
    // Come back and look again the moment the quiet period is over — nothing
    // else will fire while the socket sits closed.
    connTimer = setTimeout(renderConn, CONN_QUIET_MS - down);
    return;
  }

  connWasLoud = true;
  host.innerHTML = connCard(connState(h, down));
  host.classList.remove("hidden");
  host.querySelector("[data-act]")?.addEventListener("click", (e) => {
    if (e.currentTarget.dataset.act === "new") {
      // A fresh session is not a recovered one; it must not collect the
      // "reconnected, back in step" receipt for a transcript it never had.
      connWasLoud = false;
      onNewSession();
    } else {
      retryNow();
    }
  });
  // The escalation is time-based, so it needs a wake-up of its own; past it,
  // the counter in the copy is worth refreshing at a human pace.
  if (!h.fatal) {
    connTimer = setTimeout(renderConn, down < CONN_LOUD_MS ? CONN_LOUD_MS - down : 5000);
  }
}

/** `newSession` answers the banner's "Start a new session" button. */
export function initConnBanner({ newSession }) {
  onNewSession = newSession;
  subscribe((kind) => {
    // Leaving a conversation (Home) detaches without a connection event, and a
    // banner about a socket nobody is waiting on is just noise.
    if (kind === "connection" || kind === "reset") renderConn();
    // Proof the session is genuinely back — an `open` socket has not replayed
    // anything yet, and may still be closed on us. Only an outage the user was
    // told about earns a receipt.
    if (kind === "replay_done" && connWasLoud) {
      connWasLoud = false;
      toastOk("Reconnected — the transcript is back in step with the server.");
    }
  });
}
