// The status bar, and the parts of the composer row that mirror the same
// events: the mode and model pills, the queue strip, Stop and the queued count.

import { refreshCompositionPill, refreshProfilePill } from "./composer/pills.js";
import { perFrame } from "./frame.js";
import { store, subscribe } from "./store.js";
import { esc, fmtCost, fmtMs, fmtTokens, oneLine } from "./util.js";

const $ = (id) => document.getElementById(id);

export function shortModel(id) {
  return (id || "").split("/").pop();
}

// Messages sent while the agent is busy wait in the server's input queue. The
// state event carries only the depth, so the strip keeps the texts it saw on
// the queued_message events and lets the depth trim them as they drain.
let queuedTexts = [];

function renderQueue() {
  const strip = $("queue-strip");
  strip.classList.toggle("hidden", !queuedTexts.length);
  strip.innerHTML = queuedTexts
    .map((t) => `<div class="q-item">⇢ ${esc(oneLine(t, 90))}</div>`).join("");
}

/** The queue belongs to the conversation: a new one starts with none. */
export function clearQueue() {
  queuedTexts = [];
  renderQueue();
}

function refreshState() {
  const s = store.state;
  if (!s) return;
  if (s.queued < queuedTexts.length) {
    queuedTexts = s.queued ? queuedTexts.slice(-s.queued) : [];
    renderQueue();
  }
  $("st-model").textContent = s.model;
  $("model-pill").textContent = shortModel(s.model) + " ▾";
  const pill = $("mode-pill");
  pill.textContent = s.mode + " ▾";
  pill.className = "pill mode-" + s.mode.replace(/[^a-z-]/g, "");
  $("st-ctx").textContent = s.context_pct != null ? `ctx ${s.context_pct.toFixed(0)}%` : "ctx –";
  $("st-tokens").textContent =
    `▲${fmtTokens(s.ledger.input_tokens)} ▼${fmtTokens(s.ledger.output_tokens)}`;
  $("st-cost").textContent = fmtCost(s.ledger.cost_usd);
  // The composition is the third session-scoped control, so it moves with the
  // same event as the mode and the model — including whether it can be switched
  // right now, which is a fact about the agent being busy.
  refreshCompositionPill();
  // The posture is the fourth, and rides the same event for the same reason —
  // including when it moved because someone switched it on the configuration
  // page or from another window.
  refreshProfilePill();
  refreshMetrics(s);
  $("btn-interrupt").classList.toggle("hidden", !s.busy);
  const qc = $("queued-count");
  qc.classList.toggle("hidden", !s.queued);
  qc.textContent = s.queued ? `${s.queued} queued` : "";
}

// Measured numbers only. A timing this client never watched reads "–" rather
// than a plausible-looking zero.
function refreshMetrics(s) {
  const m = store.metrics;
  $("st-work").textContent = `${m.turns} turn${m.turns === 1 ? "" : "s"} · ${m.steps} step${
    m.steps === 1 ? "" : "s"}`;
  const llm = m.llmMs ? fmtMs(m.llmMs) : "–";
  const tools = m.toolMs ? fmtMs(m.toolMs) : "–";
  $("st-time").textContent = `LLM ${llm} · tools ${tools}`;
  const ttft = m.ttftMs == null ? "–" : fmtMs(m.ttftMs);
  const tps = m.tps == null ? "–" : `${m.tps.toFixed(0)} tok/s`;
  $("st-speed").textContent = `TTFT ${ttft} · ${tps}`;
  const inTok = s.ledger.input_tokens || 0;
  const cached = s.ledger.cached_tokens || 0;
  $("st-cache").textContent = inTok ? `cache ${((cached / inTok) * 100).toFixed(0)}%` : "cache –";
}

function refreshStatus(state) {
  const stEl = $("st-state");
  stEl.textContent = state;
  stEl.className = "st-seg st-state s-" + state;
}

function refreshConnection() {
  const c = $("st-conn");
  const live = store.connection === "open";
  c.classList.toggle("off", !live);
  c.title = "connection: " + store.connection;
  // A dead socket must not leave a live-looking UI behind it. Both of
  // these are frozen rather than wrong-by-design: the status segment keeps
  // whatever the last `status` event said because status events stopped
  // arriving, and Stop stays visible because the `state` event that would
  // hide it never came. Read together they say "still working", which is
  // the opposite of what is happening.
  if (!live) {
    refreshStatus("offline");
    $("btn-interrupt").classList.add("hidden");
  } else {
    // The replay repaints both from the server's own words a moment later;
    // this is just the honest holding value in between.
    refreshStatus(store.agentStatus || "idle");
  }
}

// Logged events arrive in bursts (a long turn's tool calls, a whole replay),
// and the counts only need to be right when the screen next paints.
const scheduleMetrics = perFrame(() => {
  if (store.state) refreshMetrics(store.state);
});

export function initStatusBar() {
  subscribe((kind, ev) => {
    if (kind === "state") refreshState();
    if (kind === "status") { refreshStatus(ev.state); refreshCompositionPill(); }
    if (kind === "queued") { queuedTexts.push(ev.text); renderQueue(); }
    // Counts accumulate while a session replays, but the state event that
    // drives the status bar arrives before the replay does — without this the
    // bar reads "0 turns" over a fully rendered conversation. Mid-replay the
    // counts are partial, so they are drawn once, when it is done.
    if (kind === "replay_done" && store.state) refreshMetrics(store.state);
    if (kind === "event" && !store.replaying) scheduleMetrics();
    if (kind === "connection") refreshConnection();
  });
}
