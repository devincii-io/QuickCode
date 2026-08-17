// Boot and routing. QuickCode is a two-view single-page app: a Home view that
// lists projects, and a workspace (chat + side panel) bound to exactly one
// project and one conversation.
//
// The launch fragment carries the token and, when the CLI opened a directory,
// the project id — api.initAuth() strips it immediately, so navigation after
// boot never touches the URL. A project id in the fragment is the only thing
// that skips Home; otherwise the last project is merely offered there.

import { api, currentProject, initAuth, setProject } from "./api.js";
import { initChat } from "./chat.js";
import { initComposer } from "./composer.js";
import { initHome, refreshHome, rememberProject } from "./home.js";
import { initReviews, openHelp, openSessionMenu, openSettings } from "./modals.js";
import { initPanel, openPanelTab, setPanelProject } from "./panel.js";
import { store, subscribe } from "./store.js";
import { initTrajectory, selectSeq } from "./trajectory.js";
import { applyTheme, debounce, fmtCost, fmtTokens, oneLine, wireLogo } from "./util.js";
import { connect, disconnect } from "./ws.js";

const $ = (id) => document.getElementById(id);

// ---- view switching ----

function showHome() {
  disconnect();
  // No project is "current" on Home, so the global modals (settings, help)
  // fall back to the unscoped routes, which address the launch project.
  setProject(null);
  document.getElementById("app").classList.add("showing-home");
  document.title = "QuickCode";
  refreshHome();
}

function showWorkspace() {
  document.getElementById("app").classList.remove("showing-home");
}

// ---- status bar + pills ----

function shortModel(id) {
  return (id || "").split("/").pop();
}

function refreshState() {
  const s = store.state;
  if (!s) return;
  $("st-model").textContent = s.model;
  $("model-pill").textContent = shortModel(s.model) + " ▾";
  const pill = $("mode-pill");
  pill.textContent = s.mode + " ▾";
  pill.className = "pill mode-" + s.mode.replace(/[^a-z-]/g, "");
  $("st-ctx").textContent = s.context_pct != null ? `ctx ${s.context_pct.toFixed(0)}%` : "ctx –";
  $("st-tokens").textContent =
    `▲${fmtTokens(s.ledger.input_tokens)} ▼${fmtTokens(s.ledger.output_tokens)}`;
  $("st-cost").textContent = fmtCost(s.ledger.cost_usd);
  $("btn-interrupt").classList.toggle("hidden", !s.busy);
  const qc = $("queued-count");
  qc.classList.toggle("hidden", !s.queued);
  qc.textContent = s.queued ? `${s.queued} queued` : "";
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
}

async function openConversation(resume) {
  const pid = currentProject();
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
  const { token, project, resumeHint } = initAuth();

  initChat({
    openTrace: (seq) => { openPanelTab("trajectory"); selectSeq(seq, { scroll: true }); },
  });
  initTrajectory();
  initPanel();
  initReviews();
  initComposer({ onNewConversation: () => openConversation(null) });
  initHome({ onOpen: (proj, opts) => openProject(proj, opts) });
  wireLogo($("brand-home").querySelector("img"), "brand-mark-fallback");

  $("brand-home").addEventListener("click", showHome);
  // Settings and help are install-wide, so Home carries them too.
  $("home-settings").addEventListener("click", () => openSettings());
  $("home-help").addEventListener("click", () => openHelp());
  $("btn-new-chat").addEventListener("click", () => openConversation(null));
  $("btn-settings").addEventListener("click", () => openSettings());
  $("session-chip").addEventListener("click", (e) =>
    openSessionMenu(e.currentTarget, {
      onPick: (convId) => openConversation(convId),
      onNew: () => openConversation(null),
    }));

  subscribe((kind, ev) => {
    if (kind === "state") refreshState();
    if (kind === "status") refreshStatus(ev.state);
    if (kind === "event" && ev.type === "user_message") bumpSessionChip();
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
    return;
  }
  showHome();
}

boot();
