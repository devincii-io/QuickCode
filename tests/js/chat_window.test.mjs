import test from "node:test";
import assert from "node:assert/strict";
import {
  CHUNK, KEEP, TranscriptWindow, revealStart, tailStart, trimStart,
} from "../../quickcode/frontend/js/chat/window.js";

// Just enough DOM for the window: nodes with a parent, fragments that empty
// into whatever they are inserted into, and a scroller that lays its children
// out 10px apart.
class Node {
  constructor(name) { this.name = name; this.parent = null; this.children = []; }
  get isConnected() { return !!this.parent; }
  get nextSibling() {
    const sibs = this.parent?.children;
    return sibs ? sibs[sibs.indexOf(this) + 1] || null : null;
  }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    const moving = n instanceof Fragment ? n.children.splice(0) : [n];
    for (const m of moving) {
      m.remove();
      const at = ref ? this.children.indexOf(ref) : this.children.length;
      this.children.splice(at, 0, m);
      m.parent = this;
    }
    return n;
  }
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  addEventListener(type, fn) { this.onclick = fn; }
  getBoundingClientRect() {
    const root = this.parent;
    return { top: root.children.indexOf(this) * 10 - root.scrollTop };
  }
}
class Fragment extends Node {}
const doc = {
  createDocumentFragment: () => new Fragment("#fragment"),
  createElement: (tag) => new Node(tag),
};
function scroller() {
  const root = new Node("transcript");
  root.ownerDocument = doc;
  root.scrollTop = 0;
  root.scrollTo = ({ top }) => { root.scrollTop = top; };
  return root;
}
const names = (root) => root.children.map((n) => n.name);
const blocks = (n, from = 0) => Array.from({ length: n }, (_, i) => new Node(`b${from + i}`));

test("the ranges: newest KEEP after a replay, a CHUNK per reveal, trims in whole chunks", () => {
  assert.equal(tailStart(50), 0);
  assert.equal(tailStart(KEEP + 30), 30);
  assert.equal(revealStart(30), 0);
  assert.equal(revealStart(CHUNK + 5), 5);
  assert.equal(trimStart(0, KEEP + CHUNK - 1), 0);
  assert.equal(trimStart(0, KEEP + CHUNK), CHUNK);
});

test("a replay is built off-document and attaches only its newest blocks", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  win.defer();
  const all = blocks(KEEP + 50);
  all.forEach((b) => win.append(b));
  assert.equal(root.children.length, 0);
  win.settle();
  assert.equal(root.children.length, KEEP + 1);
  assert.equal(root.children[0], win.more);
  assert.equal(win.more.textContent, "show 50 earlier items");
  assert.deepEqual(names(root).slice(1), all.slice(50).map((b) => b.name));
  // Later events attach at once.
  const live = new Node("live");
  win.append(live);
  assert.equal(root.children.at(-1), live);
});

test("a short replay attaches everything and offers nothing earlier", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  win.defer();
  blocks(5).forEach((b) => win.append(b));
  win.settle();
  assert.deepEqual(names(root), ["b0", "b1", "b2", "b3", "b4"]);
  assert.equal(win.more, null);
});

test("revealing brings back the previous chunk in order and keeps the view still", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  win.defer();
  const all = blocks(KEEP + CHUNK + 10);
  all.forEach((b) => win.append(b));
  win.settle();
  const anchor = all[CHUNK + 10];
  root.scrollTop = 0;
  const before = anchor.getBoundingClientRect().top;
  assert.equal(win.revealOlder(), true);
  assert.deepEqual(names(root).slice(1, 3), ["b10", "b11"]);
  assert.equal(win.more.textContent, "show 10 earlier items");
  // No scroll anchoring in this "browser": the window moved the page itself.
  assert.equal(anchor.getBoundingClientRect().top, before);
  assert.equal(win.revealOlder(), true);
  assert.equal(win.more, null);
  assert.deepEqual(names(root), all.map((b) => b.name));
  assert.equal(win.revealOlder(), false);
});

test("a reader following a long live session sheds the backlog above in chunks", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  blocks(KEEP + CHUNK - 1).forEach((b) => win.append(b));
  win.trim();
  assert.equal(root.children.length, KEEP + CHUNK - 1);
  win.append(new Node("one more"));
  win.trim();
  assert.equal(root.children.length, KEEP + 1);          // the button and KEEP blocks
  assert.equal(win.more.textContent, `show ${CHUNK} earlier items`);
  assert.equal(root.children.at(-1).name, "one more");
});

test("removing the live bubble keeps the ranges straight, attached or not", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  win.defer();
  blocks(KEEP + 3).forEach((b) => win.append(b));
  const bubble = new Node("bubble");
  win.append(bubble);
  win.remove(bubble);                    // superseded before the replay ended
  win.settle();
  assert.equal(win.more.textContent, "show 3 earlier items");
  assert.ok(!names(root).includes("bubble"));
  const live = new Node("live bubble");
  win.append(live);
  win.remove(live);
  assert.equal(live.isConnected, false);
  assert.equal(win.blocks.length, KEEP + 3);
  win.remove(new Node("never a block"));   // harmless
});

test("the button reveals, and every block stays reachable for a copy", () => {
  const root = scroller();
  const win = new TranscriptWindow(root);
  win.defer();
  const all = blocks(KEEP + 1);
  all.forEach((b) => win.append(b));
  win.settle();
  assert.deepEqual(win.all(), all);
  win.more.onclick();
  assert.equal(win.more, null);
  assert.equal(root.children.length, KEEP + 1);
});
