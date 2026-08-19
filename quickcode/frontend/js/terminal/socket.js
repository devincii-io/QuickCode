// The terminal's WebSocket. Deliberately its own socket, and deliberately its
// own file.
//
// Not folded into ws.js: that socket carries the conversation, and its whole
// contract is "exactly one may reach the store at a time", with a generation
// counter, a replay protocol and a store reset per connect. A shell has none of
// those problems and one the conversation does not have — reconnecting means a
// *new shell*, because the old process was killed when the socket dropped.
// Sharing the transport would mean sharing the reconnect policy, and the right
// policy for the two is not the same one.
//
// The token rides as a subprotocol, exactly as ws.js sends it: browsers cannot
// set headers on a WebSocket, and this endpoint is behind the same loopback
// token as every other one.

import { authToken } from "../api.js";

const CLOSE_GONE = 4404;      // no such project
const CLOSE_DENIED = 4403;    // host, origin or token refused
const CLOSE_NO_SHELL = 4500;  // the shell would not start

export class TerminalSocket {
  constructor({ onOutput, onReady, onStatus }) {
    this.onOutput = onOutput;
    this.onReady = onReady;
    this.onStatus = onStatus;      // (state, detail) — for the panel's header
    this.ws = null;
    this.projectId = null;
    this.generation = 0;
    this.pendingSize = null;
    this.sawExit = false;
  }

  open(projectId) {
    this.close();
    this.projectId = projectId;
    const mine = ++this.generation;
    // The server announces the exit and *then* closes. Without this the close
    // handler overwrote "shell ended — exit 0" with a bare "disconnected",
    // throwing away the only interesting half of the news.
    this.sawExit = false;
    this.onStatus("starting", "");
    const proto = authToken() ? ["qcauth." + authToken()] : undefined;
    const url = projectId
      ? `ws://${location.host}/ws/projects/${encodeURIComponent(projectId)}/terminal`
      : `ws://${location.host}/ws/terminal`;
    let sock;
    try {
      sock = new WebSocket(url, proto);
    } catch (err) {
      this.onStatus("failed", String(err));
      return;
    }
    this.ws = sock;

    sock.onmessage = (m) => {
      if (mine !== this.generation) return;
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      if (ev.type === "output") this.onOutput(ev.data);
      else if (ev.type === "terminal_ready") {
        this.onStatus("live", ev.cwd || "");
        this.onReady(ev);
        // The size was measured before the socket existed; tell the pty now.
        if (this.pendingSize) this.resize(this.pendingSize.rows, this.pendingSize.cols);
      } else if (ev.type === "exit") {
        this.sawExit = true;
        this.onStatus("exited", ev.code === null ? "" : `exit ${ev.code}`);
      } else if (ev.type === "terminal_error") {
        this.onStatus("failed", ev.message || "");
      }
    };
    sock.onclose = (e) => {
      if (mine !== this.generation) return;
      this.ws = null;
      // No automatic reconnect, on purpose. A dropped conversation socket can
      // be resumed from the log; a dropped terminal cannot — the shell is
      // dead and its state died with it. Silently starting a fresh one would
      // put a clean prompt where an unfinished command used to be. The panel
      // offers a button instead.
      if (e.code === CLOSE_DENIED) this.onStatus("denied", "");
      else if (e.code === CLOSE_GONE) this.onStatus("gone", "");
      else if (e.code === CLOSE_NO_SHELL) this.onStatus("failed", "no shell available");
      else if (!this.sawExit) this.onStatus("closed", "");
    };
    sock.onerror = () => {};
  }

  get live() { return !!this.ws && this.ws.readyState === WebSocket.OPEN; }

  send(obj) {
    if (!this.live) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }

  input(data) { return this.send({ type: "input", data }); }

  resize(rows, cols) {
    this.pendingSize = { rows, cols };
    return this.send({ type: "resize", rows, cols });
  }

  close() {
    this.generation++;
    if (!this.ws) return;
    this.ws.onopen = this.ws.onmessage = this.ws.onclose = this.ws.onerror = null;
    try { this.ws.close(); } catch { /* already going */ }
    this.ws = null;
  }
}
