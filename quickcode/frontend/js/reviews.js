// The dialogs the agent waits on: permission review and plan review.
//
// Reviews are a QUEUE, not a single slot. Read-only tool calls in one assistant
// message run concurrently, so four of them can hit a protected path at once and
// the server opens four futures, each awaiting its own decision
// (server/reviews.py: `await fut`). Showing them one dialog at a time and
// dropping the rest left those futures pending forever: the tool calls hung and
// the turn never ended. Every request is now either answered or still queued.

import { api } from "./api.js";
import { diffNode, unifiedLines } from "./diff.js";
import { explainErrorHtml, explainHtml } from "./help/explain.js";
import { offerSummary, SETTINGS_FILE } from "./permission_offer.js";
import { store, subscribe } from "./store.js";
import { closeModal, modal, modalRoot, onModalClose } from "./ui/modal.js";
import { esc } from "./util.js";
import { actions } from "./ws.js";

const queue = [];              // reviews waiting for an answer, oldest first
const queued = new Set();      // req_ids in `queue`, so replay cannot double-add
const decided = new Set();     // req_ids already answered from here
let current = null;            // the queue entry currently on screen

export function initReviews() {
  // Anything that opens a modal wipes the one on screen. If that was a review,
  // the agent is still blocked on it, so hand it back to the queue instead of
  // losing it — it reopens on top of whatever displaced it.
  onModalClose(reclaimReview);
  subscribe((kind, ev) => {
    if (kind === "reset") return resetReviews();
    if (kind === "event" && ev.type === "permission_request") enqueue("permission", ev);
    if (kind === "event" && ev.type === "plan_request") enqueue("plan", ev);
    if (kind === "state" && ev.pending) {
      // The authoritative list of what the server is still blocked on: it
      // recovers anything a reconnect or a lost frame would otherwise strand.
      for (const p of ev.pending) enqueue(p.kind, p);
    }
    if (kind === "event" && (ev.type === "permission_resolved" || ev.type === "plan_resolved")) {
      drop(ev.req_id);
    }
  });
}

function resetReviews() {
  queue.length = 0;
  queued.clear();
  decided.clear();
  current = null;
  closeModal();
}

function enqueue(kind, ev) {
  if (!ev?.req_id || queued.has(ev.req_id) || decided.has(ev.req_id)) return;
  // During replay only surface requests the server still reports as pending.
  if (store.replaying && !(store.state?.pending || []).some((p) => p.req_id === ev.req_id)) return;
  queue.push({ kind, ev });
  queued.add(ev.req_id);
  showNext();
  refreshWaiting();
}

// Answered, or resolved by something other than this dialog (another tab, an
// interrupt): forget it either way.
function drop(reqId) {
  const i = queue.findIndex((q) => q.ev.req_id === reqId);
  if (i >= 0) queue.splice(i, 1);
  queued.delete(reqId);
  if (current?.ev.req_id === reqId) {
    current = null;
    closeModal();
  }
  showNext();
  refreshWaiting();
}

// Something else opened a modal over a live review. The agent is still waiting,
// so the review goes back on screen once the displacing dialog has settled.
function reclaimReview() {
  if (!current) return;
  current = null;
  queueMicrotask(showNext);
}

function showNext() {
  if (current || !queue.length) return;
  const item = queue[0];   // stays queued while on screen; only a decision pops it
  if (item.kind === "permission") permissionModal(item.ev);
  else planModal(item.ev);
  current = item;
  refreshWaiting();
}

function answered(reqId) {
  decided.add(reqId);
  queued.delete(reqId);
  const i = queue.findIndex((q) => q.ev.req_id === reqId);
  if (i >= 0) queue.splice(i, 1);
  current = null;
  closeModal();
  showNext();
}

// "2 more waiting" is the difference between "the agent asked" and "the agent is
// blocked on four things"; with a fan-out of subagents that is the whole story.
// The node is always rendered and filled in afterwards: the requests that pile
// up behind this one arrive *after* its dialog is on screen.
const WAITING_NODE =
  `<div data-rv-waiting style="font-size:12px;color:var(--warning);margin-bottom:8px"></div>`;

function refreshWaiting() {
  const node = modalRoot().querySelector("[data-rv-waiting]");
  if (!node) return;
  const more = queue.length - 1;
  node.textContent = more > 0
    ? `${more} more request${more === 1 ? "" : "s"} waiting behind this one`
    : "";
}

// Answering a review puts the next one on screen at once, in the same place,
// with the same buttons — so the second click of a double-click, or a click
// aimed at whatever was there a moment earlier, used to land on a request
// nobody had read and could approve it. A review ignores clicks for a beat
// after it appears.
const REVIEW_ARM_MS = 400;

function armed(m) {
  const shownAt = performance.now();
  return () => m.isConnected && performance.now() - shownAt >= REVIEW_ARM_MS;
}

// Built from nodes and text rather than markup: every string here — the
// command, the diff, the rules, a hook's reason — came from the model, a file
// or a hook script, not from us.
function node(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function permissionBody(ev) {
  const lead = node("div", "perm-lead", "The agent wants to run ");
  lead.append(node("span", "perm-tool", ev.tool));
  if (ev.agent && ev.agent !== "main") lead.append(` (subagent ${ev.agent})`);
  const parts = [lead];
  if (ev.hook_reason) {
    const hook = node("div", "perm-hook", "A PreToolUse hook asked for this: ");
    hook.append(node("span", "perm-hook-reason", ev.hook_reason));
    parts.push(hook);
  }
  parts.push(node("div", "perm-preview", ev.preview || ev.arg));
  if (ev.diff) {
    const box = node("div", "perm-diff");
    box.append(diffNode(unifiedLines(ev.diff)));
    parts.push(box);
  }
  parts.push(offerNode(offerSummary(ev)), ...whyNodes(ev));
  return parts;
}

// The exact rules "Always allow" writes, shown before anyone clicks it — and
// the parts of the call no rule can cover, so nobody expects a rule to.
function offerNode(offer) {
  const box = node("div", "perm-offer");
  if (offer.canSave) {
    box.append(node("div", null, offer.rules.length === 1
      ? "Always allow saves this rule to " : "Always allow saves these rules to "));
    box.firstChild.append(node("code", null, SETTINGS_FILE), ":");
    const list = node("ul", "perm-rules");
    for (const rule of offer.rules) list.appendChild(node("li")).append(node("code", null, rule));
    box.append(list);
  } else {
    box.append(node("div", null, offer.empty));
  }
  if (offer.kept.length) {
    box.append(node("div", "perm-kept-head", "Still asks next time, whatever is saved:"));
    const list = node("ul", "perm-kept");
    for (const k of offer.kept) {
      list.appendChild(node("li")).append(node("code", null, k.part), ` — ${k.why}`);
    }
    box.append(list);
  }
  return box;
}

// "Why?" asks the permission engine about this very prompt — the pending call,
// the gate of the agent that raised it (server/permissions_api.py) — and draws
// the same trace the Help sandbox does.
function whyNodes(ev) {
  const toggle = node("button", "ghost-btn perm-why-toggle", "Why am I being asked?");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", "false");
  const out = node("div", "perm-why hidden");
  let asked = false;
  toggle.addEventListener("click", async () => {
    const open = out.classList.toggle("hidden") === false;
    toggle.setAttribute("aria-expanded", String(open));
    if (!open || asked) return;
    asked = true;
    out.textContent = "Asking the permission engine…";
    try {
      out.innerHTML = explainHtml(
        await api.explainPermission({ conv: store.convId, review: ev.req_id }));
    } catch (err) {
      asked = false;
      out.innerHTML = explainErrorHtml(err);
    }
  });
  return [toggle, out];
}

function permissionModal(ev) {
  const offer = offerSummary(ev);
  const m = modal(
    "Permission required",
    `${WAITING_NODE}<div data-perm-body></div>
     <input class="deny-input hidden" placeholder="Why not? (optional — steers the agent)">`,
    `<button class="btn danger" data-act="deny">Deny</button>
     <button class="btn" data-act="always">Always allow</button>
     <button class="btn primary" data-act="allow">Allow once</button>`,
    { dismissible: false }
  );
  m.querySelector("[data-perm-body]").append(...permissionBody(ev));
  if (!offer.canSave) {
    const always = m.querySelector('[data-act="always"]');
    always.disabled = true;
    always.title = offer.empty;
  }
  const denyInput = m.querySelector(".deny-input");
  const ready = armed(m);
  m.querySelector(".modal-foot").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act || !ready()) return;
    if (act === "deny" && denyInput.classList.contains("hidden")) {
      denyInput.classList.remove("hidden");
      denyInput.focus();
      e.target.textContent = "Confirm deny";
      return;
    }
    if (act === "allow") actions.permissionDecision(ev.req_id, true, false);
    if (act === "always") actions.permissionDecision(ev.req_id, true, true);
    if (act === "deny") actions.permissionDecision(ev.req_id, false, false, denyInput.value.trim());
    answered(ev.req_id);
  });
  return m;
}

function planModal(ev) {
  const m = modal(
    "Plan review",
    `${WAITING_NODE}
     <div class="perm-preview" style="max-height:52vh">${esc(ev.plan)}</div>
     <input class="deny-input hidden" placeholder="Feedback for the next iteration…">`,
    `<button class="btn" data-act="revise">Keep planning</button>
     <button class="btn" data-act="approve-ask">Approve · ask mode</button>
     <button class="btn primary" data-act="approve-auto">Approve · auto-edit</button>`,
    { dismissible: false }
  );
  const fb = m.querySelector(".deny-input");
  const ready = armed(m);
  m.querySelector(".modal-foot").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act || !ready()) return;
    if (act === "revise" && fb.classList.contains("hidden")) {
      fb.classList.remove("hidden"); fb.focus();
      e.target.textContent = "Send feedback";
      return;
    }
    if (act === "approve-ask") actions.planDecision(ev.req_id, true, "ask");
    if (act === "approve-auto") actions.planDecision(ev.req_id, true, "auto-edit");
    if (act === "revise") actions.planDecision(ev.req_id, false, null, fb.value.trim());
    answered(ev.req_id);
  });
  return m;
}
