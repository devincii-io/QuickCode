import test, { mock } from "node:test";
import assert from "node:assert/strict";

// ws.js wires window/document listeners at import and toasts through the DOM,
// so the globals it touches are stood up before it loads.
let now = 1000;
Object.defineProperty(globalThis, "performance", { value: { now: () => now }, configurable: true });
const toasts = [];
function node() {
  return {
    children: [], isConnected: true, dataset: {}, classList: { add() {} },
    setAttribute() {}, addEventListener() {}, appendChild() {}, remove() {},
    querySelector: () => node(),
    set innerHTML(html) { toasts.push(html); },
  };
}
globalThis.window = { addEventListener() {} };
globalThis.document = {
  visibilityState: "visible", body: node(), addEventListener() {}, createElement: node,
};
globalThis.location = { host: "127.0.0.1:1", hash: "", search: "" };

const sockets = [];
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    this.sent = [];
    this.closed = null;
    sockets.push(this);
  }
  send(data) { this.sent.push(data); }
  close(code) { this.closed = code ?? 1000; this.readyState = FakeSocket.CLOSED; }
  // What the browser does when the server's first frame lands.
  live() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
    this.onmessage?.({ data: JSON.stringify({ type: "heartbeat" }) });
  }
}
globalThis.WebSocket = FakeSocket;

mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
const { actions, connect, retryNow } = await import("../../quickcode/frontend/js/ws.js");

test("a socket revived by retryNow is still watched for silence", () => {
  connect("p", "c");
  sockets.at(-1).live();
  // The socket drops; a focus event revives it before the backoff fires.
  const dead = sockets.at(-1);
  dead.readyState = FakeSocket.CLOSED;
  dead.onclose({ code: 1006 });
  retryNow();
  const revived = sockets.at(-1);
  assert.notEqual(revived, dead);
  revived.live();
  // Then it goes quiet the way a zombie does after sleep: OPEN, no frames.
  now += 60_000;
  mock.timers.tick(5_000);
  assert.equal(revived.closed, 4001, "the watchdog should have closed the silent socket");
});

test("a frame over the server's size limit is refused instead of silently lost", () => {
  connect("p", "c");
  const sock = sockets.at(-1);
  sock.live();
  toasts.length = 0;
  assert.equal(actions.userMessage("x".repeat(17 * 1024 * 1024)), false);
  assert.equal(sock.sent.length, 0);
  assert.match(toasts.join(""), /Too large to send: 17\.0 MB/);
  assert.equal(actions.userMessage("fine"), true);
  assert.deepEqual(JSON.parse(sock.sent[0]), { type: "user_message", text: "fine" });
  // Multi-byte text is measured in bytes, which is what the server counts.
  assert.equal(actions.userMessage("é".repeat(9 * 1024 * 1024)), false);
});
