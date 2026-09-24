// The dialogs the agent waits on: permission review and plan review.
//
// Reviews are a QUEUE, not a single slot. Read-only tool calls in one assistant
// message run concurrently, so four of them can hit a protected path at once and
// the server opens four futures, each awaiting its own decision
// (server/manager.py: `await fut`). Showing them one dialog at a time and
// dropping the rest left those futures pending forever: the tool calls hung and
// the turn never ended. Every request is now either answered or still queued.

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

function permissionModal(ev) {
  const m = modal(
    "Permission required",
    `${WAITING_NODE}
     <div>The agent wants to run
       <span class="perm-tool">${esc(ev.tool)}</span>
       ${ev.agent && ev.agent !== "main" ? `(subagent ${esc(ev.agent)})` : ""}</div>
     <div class="perm-preview">${esc(ev.preview || ev.arg)}</div>
     <div style="font-size:12px;color:var(--fg-dim)">Always-allow saves the rule
       <code>${esc(ev.rule_suggestion)}</code> to .quickcode/settings.local.json</div>
     <input class="deny-input hidden" placeholder="Why not? (optional — steers the agent)">`,
    `<button class="btn danger" data-act="deny">Deny</button>
     <button class="btn" data-act="always">Always allow</button>
     <button class="btn primary" data-act="allow">Allow once</button>`,
    { dismissible: false }
  );
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
