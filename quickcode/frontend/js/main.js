// Boot and routing. QuickCode is a three-view single-page app: a Home view
// that lists projects, a workspace (chat + side panel) bound to exactly one
// project and one conversation, and Configuration — a peer view rather than a
// dialog, addressed by `#/config/…` so every page in it has a URL.
//
// The launch fragment carries the token and, when the CLI opened a directory,
// the project id — api.initAuth() strips it immediately, so navigation after
// boot never touches the URL. A project id in the fragment is the only thing
// that skips Home; otherwise the last project is merely offered there.

import { api, currentProject, initAuth, setProject } from "./api.js";
import { initChat } from "./chat.js";
import { initComposer, refreshCompositionPill } from "./composer.js";
import { initHome, refreshHome, rememberProject } from "./home.js";
import { initReviews, openHelp, openQuickSettings, openSessionMenu } from "./modals.js";
import {
  DEFAULT_ROUTE, initConfig, invalidate as invalidateConfig, isConfigRoute,
  lastConfigRoute, render as renderConfig,
} from "./config/view.js";
import { initPanel, openPanelTab, setPanelProject } from "./panel.js";
import { store, subscribe } from "./store.js";
import { setInspector } from "./inspect.js";
import { checkTrust, initTrust, resetTrust } from "./trust.js";
import { initTrajectory, selectSeq } from "./trajectory.js";
import { applyTheme, debounce, esc, fmtCost, fmtMs, fmtTokens, oneLine, wireLogo } from "./util.js";
import { connect, disconnect } from "./ws.js";

const $ = (id) => document.getElementById(id);

// ---- view switching ----

// Which view was showing before Configuration took over, so "Done" goes back
// to what the user was doing rather than to an arbitrary home.
let cameFrom = "home";

function showHome() {
  leaveConfig();
  disconnect();
  // The trust banner belongs to one project; leaving the workspace drops it so
  // the next project is asked about rather than inheriting an answer.
  resetTrust();
  // No project is "current" on Home, so the global modals (quick settings,
  // help) fall back to the unscoped routes, which address the launch project.
  setProject(null);
  document.getElementById("app").classList.add("showing-home");
  document.title = "QuickCode";
  refreshHome();
}

function showWorkspace() {
  leaveConfig();
  const app = document.getElementById("app");
  app.classList.remove("showing-home");
  // The transcript grew while it was hidden, where every scroll measurement
  // reads zero; put it back at the bottom rather than at an accidental offset.
  const t = $("transcript");
  if (t) t.scrollTop = t.scrollHeight;
}

/** Configuration is a view, and this is the whole reason it is one:
 *  it never calls disconnect(). The workspace stays mounted and hidden with
 *  its socket open, so changing a setting does not cost the session. */
function showConfig(route) {
  const app = document.getElementById("app");
  if (!app.classList.contains("showing-config")) {
    cameFrom = app.classList.contains("showing-home") ? "home" : "workspace";
  }
  app.classList.add("showing-config");
  document.title = "QuickCode — Configuration";
  if (route && location.hash !== route) { location.hash = route; return; }
  renderConfig();
}

function leaveConfig() {
  const app = document.getElementById("app");
  if (!app.classList.contains("showing-config")) return;
  app.classList.remove("showing-config");
  if (isConfigRoute(location.hash)) {
    history.replaceState(null, "", location.pathname + location.search);
  }
}

function closeConfig() {
  if (cameFrom === "home") { showHome(); return; }
  showWorkspace();
  document.title = store.bootstrap?.project
    ? `QuickCode — ${store.bootstrap.project}` : "QuickCode";
}

// ---- status bar + pills ----

function shortModel(id) {
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

// The chip shows the session's own title, which the backend derives from the
// first user message — so it only becomes meaningful after that message lands.
async function refreshSessionChip() {
  const chip = $("session-chip");
  const convId = store.convId;
  let title = "New session";
  try {
    const sessions = await api.sessions();
    const mine = sessions.find((s) => s.conv_id === convId);
    if (mine?.title) title = oneLine(mine.title, 42);
  } catch { /* offline: keep the placeholder */ }
  if (store.convId !== convId) return;   // switched while we were asking
  chip.textContent = title + " ▾";
  chip.title = `Session ${convId} — click to switch`;
}

const bumpSessionChip = debounce(refreshSessionChip, 800);

// ---- project + conversation lifecycle ----

async function openProject(project, { resume = null } = {}) {
  setProject(project.id);
  setPanelProject(project.id);
  rememberProject(project.id);
  showWorkspace();

  $("project-chip").textContent = project.name || "…";
  $("session-chip").textContent = "New session ▾";

  let bs;
  try {
    bs = await api.bootstrap();
  } catch (err) {
    showHome();
    $("home-projects").insertAdjacentHTML("afterbegin",
      `<div class="home-err">Could not open that project (${err.message}).</div>`);
    return;
  }
  store.bootstrap = bs;
  applyTheme(bs.theme);
  const chip = $("project-chip");
  chip.textContent = bs.project + (bs.git_branch ? ` · ${bs.git_branch}` : "");
  chip.title = bs.cwd;
  $("model-pill").textContent = shortModel(bs.default_model) + " ▾";
  document.title = `QuickCode — ${bs.project}`;

  await openConversation(resume);

  // Opening a project can no longer start its MCP servers on its own. Ask what
  // was refused and put it in front of the user — a project that silently
  // loses its tools is the failure mode this whole gate has to avoid.
  checkTrust(project.id);
}

async function openConversation(resume) {
  const pid = currentProject();
  queuedTexts = [];             // the queue belongs to the conversation
  renderQueue();
  try {
    const { conv_id } = await api.openConversation(resume || undefined);
    connect(pid, conv_id);
  } catch (err) {
    $("transcript").innerHTML =
      `<div class="err-note">Could not start a conversation (${err.message}).</div>`;
    return;
  }
  refreshSessionChip();
}

// ---- boot ----

async function boot() {
  // Read before initAuth(), which drops the launch fragment: a reload of a
  // linked configuration page must land back on that page.
  const wantsConfig = isConfigRoute(location.hash);
  const { token, project, resumeHint } = initAuth();

  // One inspector for every surface: chat, the agents panel and the
  // trajectory table all land on the same Summary/Payload/Result/Timing view.
  const openTrace = (seq) => {
    openPanelTab("trajectory");
    selectSeq(seq, { scroll: true });
  };
  setInspector(openTrace);
  initChat({ openTrace });
  initTrajectory();
  initPanel();
  initReviews();
  initComposer({ onNewConversation: () => openConversation(null) });
  // Freshly trusted MCP tools only reach a conversation that starts after the
  // grant, so the trust card can offer that restart itself.
  initTrust({ onNewConversation: () => openConversation(null) });
  initHome({ onOpen: (proj, opts) => openProject(proj, opts) });
  wireLogo($("brand-home").querySelector("img"), "brand-mark-fallback");
  wireLogo($("config-brand").querySelector("img"), "brand-mark-fallback");

  $("brand-home").addEventListener("click", showHome);
  // Configuration and help are install-wide, so Home carries them too.
  initConfig({ api, onDone: closeConfig });
  $("config-brand").addEventListener("click", closeConfig);
  $("home-settings").addEventListener("click", () => showConfig(lastConfigRoute()));
  $("home-help").addEventListener("click", () => openHelp());
  $("btn-new-chat").addEventListener("click", () => openConversation(null));
  $("btn-settings").addEventListener("click", () => showConfig(lastConfigRoute()));
  $("btn-quick-settings").addEventListener("click", () =>
    openQuickSettings({ onFull: () => showConfig(DEFAULT_ROUTE) }));

  // Every configuration page is a URL. A hash that names one shows the view;
  // anything else (including "back" out of it) returns to where we were.
  window.addEventListener("hashchange", () => {
    if (isConfigRoute(location.hash)) showConfig();
    else if (document.getElementById("app").classList.contains("showing-config")) closeConfig();
  });
  $("session-chip").addEventListener("click", (e) =>
    openSessionMenu(e.currentTarget, {
      onPick: (convId) => openConversation(convId),
      onNew: () => openConversation(null),
    }));

  subscribe((kind, ev) => {
    if (kind === "state") refreshState();
    if (kind === "status") { refreshStatus(ev.state); refreshCompositionPill(); }
    if (kind === "queued") { queuedTexts.push(ev.text); renderQueue(); }
    if (kind === "event" && ev.type === "user_message") bumpSessionChip();
    // A switch changes what every configuration page would say about this
    // session, and the view caches the kernel for the life of a visit.
    if (kind === "event" && ev.type === "composition_changed") invalidateConfig();
    // Counts accumulate while a session replays, but the state event that
    // drives the status bar arrives before the replay does — without this the
    // bar reads "0 turns" over a fully rendered conversation.
    if (kind === "replay_done" || kind === "event") {
      if (store.state) refreshMetrics(store.state);
    }
    if (kind === "connection") {
      const c = $("st-conn");
      c.classList.toggle("off", store.connection !== "open");
      c.title = "connection: " + store.connection;
    }
  });

  if (!token) {
    showHome();
    $("home-projects").innerHTML =
      `<div class="home-err">No auth token. Open QuickCode through the URL printed
       by the CLI — it carries the token in the fragment.</div>`;
    return;
  }

  // Only a fragment-carried project skips Home.
  if (project) {
    await openProject({ id: project }, { resume: resumeHint });
  } else {
    showHome();
  }
  if (wantsConfig) showConfig();
}

boot();
