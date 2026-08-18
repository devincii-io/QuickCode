// The live activity line: one row above the composer input that says the app
// is alive while a turn is in flight.
//
//     ✳ Nebulizing… (4m 7s · ↓ 5.3k tokens · esc to interrupt)
//
// Two rules shape everything here.
//
// The first is that whimsy is only allowed where there is nothing to report.
// A random verb is honest while we are waiting on the model — nobody, the user
// included, knows what it is doing. It is a lie during a tool call, a
// compaction or a permission prompt, where the app knows exactly what is
// happening and the user needs to know it too: a spinner over a question that
// is waiting for *them* is worse than no indicator at all. So the verb is
// reserved for `sending`/`streaming`, and every other phase says the true
// thing (see PHASES below).
//
// The second is that this line must never move the transcript. It appears and
// vanishes many times a session; the space is reserved in CSS and only the
// opacity changes, so the scroll position never jumps under a reader.
//
// Everything is driven off the store — no polling, and no timer at all when
// the agent is idle.

import { escInterrupts } from "./composer.js";
import { store, subscribe } from "./store.js";
import { fmtTokens } from "./util.js";
import { VERBS } from "./verbs.js";

const $ = (id) => document.getElementById(id);

const RUNNING = new Set(["sending", "streaming", "executing_tools"]);

// Asterisk forms, cycled slowly enough to read as breathing rather than as a
// strobe. Four frames at ~7 fps.
const FRAMES = ["✳", "✻", "✽", "✻"];
const FRAME_MS = 145;
// One verb per phase, held long enough to be read; a fresh draw every second
// would be a slot machine, not a status.
const VERB_MS = 15000;
// How long a stopped line stays legible before it clears. An interrupt or an
// error that vanishes instantly reads as "nothing happened".
const HOLD_MS = 2600;

// ---- module state ----

let els = null;
let timer = null;
let holdTimer = null;
let status = "idle";       // last `status` event state
let detail = "";           // its detail ("compacting")
let terminal = "";         // "interrupted" | "error" while the line is held
let phase = "";            // the phase currently painted
let verb = "";
let verbAt = 0;
let turnStart = 0;
let tokenBase = 0;
let lastSecs = -1;
let frame = 0;
let reducedMq = null;

function reduced() { return !!reducedMq?.matches; }

// ---- formatting ----

// 12s / 1m 4s / 1h 2m 7s — Claude Code's shape, which drops units it does not
// need rather than padding a fixed clock.
export function fmtElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  if (h) return `${h}h ${m}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

// ---- what the agent is actually doing ----

function pendingRequests() {
  return store.state?.pending || [];
}

function ledgerOut() {
  return store.state?.ledger?.output_tokens || 0;
}

// The tools this round is running. The logged `tool_call` events are the truth
// once they land; a call still streaming its arguments only exists in
// pendingCalls, and naming it early is what makes the line feel immediate.
function toolNames() {
  const names = [];
  for (const c of store.runningTools.values()) if (c.name) names.push(c.name);
  for (const c of store.pendingCalls.values()) if (c.name) names.push(c.name);
  return [...new Set(names)];
}

function toolLabel() {
  const names = toolNames();
  if (!names.length) return "Running tools";
  if (names.length <= 3) return `Running ${names.join(", ")}`;
  return `Running ${names.slice(0, 3).join(", ")} +${names.length - 3}`;
}

function approvalLabel() {
  const p = pendingRequests()[0];
  if (p?.kind === "plan") return "Waiting for your plan review";
  if (p?.tool) return `Waiting for you to approve ${p.tool}`;
  return "Waiting for your approval";
}

// The phase is derived, never stored: a permission prompt outranks whatever
// the status stream last said, because the status stream describes the agent
// and the prompt describes who the app is waiting on.
function derivePhase() {
  if (terminal) return terminal;
  if (pendingRequests().length) return "approval";
  if (detail === "compacting") return "compacting";
  if (status === "executing_tools") return "tools";
  if (RUNNING.has(status)) return "waiting";
  return "";
}

// Phase → the label before the ellipsis. `waiting` is the only one that gets
// to be whimsical.
const PHASES = {
  waiting: () => verb,
  tools: toolLabel,
  compacting: () => "Compacting the conversation",
  approval: approvalLabel,
  interrupted: () => "Interrupted",
  error: () => "Stopped on an error",
};

function isActive() {
  return RUNNING.has(status) || pendingRequests().length > 0;
}

// ---- verb ----

// Held for VERB_MS, and never the same twice running: a repeat looks like the
// line froze, which is the one thing it must not look like.
function pickVerb() {
  if (!VERBS.length) { verb = "Working"; return; }
  let i = Math.floor(Math.random() * VERBS.length);
  if (VERBS[i] === verb) i = (i + 1) % VERBS.length;
  verb = VERBS[i];
  verbAt = performance.now();
}

// ---- painting ----

function metaText() {
  if (!turnStart) return "";
  const parts = [fmtElapsed(performance.now() - turnStart)];
  const produced = Math.max(0, ledgerOut() - tokenBase);
  // Output tokens for *this* turn. The session total lives in the status bar,
  // where a number that barely moves is the point; here it would read as
  // frozen. Suppressed at zero rather than shown as "↓ 0".
  if (produced > 0) parts.push(`↓ ${fmtTokens(produced)} tokens`);
  // The hint comes from the binding itself, not from a copy of it: while a
  // permission dialog is open Escape closes the dialog, so the promise is
  // dropped for exactly as long as it would be false.
  if (!terminal && escInterrupts()) parts.push("esc to interrupt");
  return `(${parts.join(" · ")})`;
}

function paintLabel() {
  const p = derivePhase();
  const label = (PHASES[p] || (() => "Working"))();
  els.verb.textContent = terminal ? label : `${label}…`;
}

function paintMeta() {
  els.meta.textContent = metaText();
}

// Screen readers get the phase, not the clock: an aria-live region over the
// whole line would re-announce the elapsed time every second and drown the
// transcript. Only a change of phase is worth an interruption — and the verb
// is not one, so the waiting phase announces what it means rather than which
// word it drew.
function announce() {
  const p = derivePhase();
  if (!p) { els.live.textContent = ""; return; }
  els.live.textContent = p === "waiting" ? "Working" : PHASES[p]();
}

function paint() {
  const p = derivePhase();
  const changed = p !== phase;
  phase = p;
  els.root.dataset.phase = p;
  paintLabel();
  paintMeta();
  if (changed) announce();
}

// ---- the clock ----

function tick() {
  if (!reduced()) {
    frame = (frame + 1) % FRAMES.length;
    els.glyph.textContent = FRAMES[frame];
  }
  const now = performance.now();
  if (phase === "waiting" && now - verbAt >= VERB_MS) { pickVerb(); paintLabel(); }
  const secs = Math.floor((now - turnStart) / 1000);
  if (secs !== lastSecs) { lastSecs = secs; paintMeta(); }
}

function startTimer() {
  stopTimer();
  // Under reduced motion there is no glyph to advance, so the only thing left
  // to serve is the clock — one tick a second instead of seven.
  timer = setInterval(tick, reduced() ? 1000 : FRAME_MS);
}

function stopTimer() {
  if (timer) { clearInterval(timer); timer = null; }
}

function clearHold() {
  if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
}

// ---- lifecycle ----

function start() {
  if (turnStart) return;                 // already running this turn
  turnStart = performance.now();
  tokenBase = ledgerOut();
  lastSecs = -1;
  frame = 0;
  els.glyph.textContent = FRAMES[0];
  pickVerb();
  els.root.classList.add("on");
  startTimer();
}

function hide() {
  stopTimer();
  clearHold();
  turnStart = 0;
  terminal = "";
  phase = "";
  els.root.classList.remove("on");
  els.root.dataset.phase = "";
  els.live.textContent = "";
}

// A finished turn: `idle` just clears, but an interrupt or an error freezes
// the glyph and holds the line for a beat so the user sees why it stopped.
function stop(kind) {
  if (!kind) { hide(); return; }
  if (!turnStart) return;                // nothing was showing
  stopTimer();
  clearHold();
  terminal = kind;
  paint();
  holdTimer = setTimeout(hide, HOLD_MS);
}

function sync() {
  if (terminal) return;                  // holding a stopped line
  if (isActive()) { start(); paint(); } else hide();
}

// ---- wiring ----

export function initActivity() {
  els = {
    root: $("activity"),
    glyph: $("activity-glyph"),
    verb: $("activity-verb"),
    meta: $("activity-meta"),
    live: $("activity-live"),
  };
  if (!els.root) return;
  reducedMq = window.matchMedia("(prefers-reduced-motion: reduce)");
  // Flipping the OS setting mid-turn re-rates the timer instead of waiting for
  // the next one.
  reducedMq.addEventListener?.("change", () => {
    if (!reduced()) return;
    els.glyph.textContent = FRAMES[0];
    if (timer) startTimer();
  });

  subscribe((kind, ev) => {
    if (kind === "status") {
      status = ev.state || "idle";
      detail = ev.detail || "";
      if (RUNNING.has(status)) { terminal = ""; clearHold(); sync(); return; }
      if (status === "interrupted" || status === "error") { stop(status); return; }
      sync();                            // idle
      return;
    }
    if (kind === "state") { sync(); return; }
    // A tool starting or finishing changes what the line names, and neither
    // carries a status flip of its own.
    if (kind === "event" && (ev.type === "tool_call" || ev.type === "tool_result")) {
      if (turnStart) paint();
      return;
    }
    if (kind === "stream" && phase === "tools" && turnStart) { paint(); return; }
    if (kind === "reset") { status = "idle"; detail = ""; hide(); return; }
    if (kind === "connection" && store.connection !== "open") {
      status = "idle";
      detail = "";
      hide();
    }
  });
}
