// Boot and routing. QuickCode is a four-view single-page app: a Home view
// that lists projects, a workspace (chat + side panel) bound to exactly one
// project and one conversation, Configuration — a peer view rather than a
// dialog, addressed by `#/config/…` so every page in it has a URL — and Help
// at `#/help/…`, on the same terms, so a Settings card can link to the
// paragraph that explains it.
//
// The launch fragment carries the token and, when the CLI opened a directory,
// the project id — api.initAuth() strips it immediately, so navigation after
// boot never touches the URL. A project id in the fragment is the only thing
// that skips Home; otherwise the last project is merely offered there.

import { initActivity } from "./activity.js";
import { api, currentProject, initAuth, setProject } from "./api.js";
import { initCopy } from "./copy.js";
import { initChat } from "./chat.js";
import { initComposer } from "./composer.js";
import { initConnBanner } from "./connbanner.js";
import { initHome, refreshHome, rememberProject } from "./home.js";
import { openHelp } from "./help/quickref.js";
import { openQuickSettings } from "./quick_settings.js";
import { initReviews } from "./reviews.js";
import { clearSessionBar, initSessionBar, refreshSessionBar } from "./sessionbar.js";
import {
  DEFAULT_ROUTE, initConfig, invalidate as invalidateConfig, isConfigRoute,
  lastConfigRoute, render as renderConfig,
} from "./config/view.js";
import {
  initHelp, isHelpRoute, lastHelpRoute, render as renderHelp,
} from "./help/view.js";
import { initPaneNotices } from "./pane_notices.js";
import { initPanel, openPanelTab, setPanelProject } from "./panel.js";
import { clearQueue, initStatusBar, shortModel } from "./statusbar.js";
import { store, subscribe } from "./store.js";
import { inspect, setInspector } from "./inspect.js";
import { initTerminal, setTerminalProject } from "./terminal/panel.js";
import { checkTrust, initTrust, resetTrust } from "./trust.js";
import { initTrajectory, selectSeq } from "./trajectory.js";
import { applyTheme, esc, wireLogo } from "./util.js";
import { connect, disconnect } from "./ws.js";

const $ = (id) => document.getElementById(id);
const embedded = window.parent !== window && new URLSearchParams(location.search).get("pane") === "1";
const utility = new URLSearchParams(location.search).get("utility") === "1";
const pendingReviews = new Set();
function tellWorkspace(action, data = {}) {
  if (embedded) window.parent.postMessage({ source: "qc-agent", action, ...data }, location.origin);
}
function reportPane(title) {
  tellWorkspace("state", {
    convId: store.convId,
    ...(title ? { title } : {}),
    connection: store.connection, busy: !!store.state?.busy,
    review: pendingReviews.size > 0,
    persisted: store.events.some((e) => e.type === "user_message"),
  });
}

// ---- view switching ----

// Which view was showing before Configuration took over, so "Done" goes back
// to what the user was doing rather than to an arbitrary home.
let cameFrom = "home";

function showHome() {
  if (embedded && !utility && store.convId) { tellWorkspace("home"); return; }
  leaveConfig();
  leaveHelp();
  disconnect();
  // The terminal is a live shell in *this* project's directory. Leaving the
  // workspace closes its socket, which is what kills the process tree -- a
  // shell left running against a project nobody is looking at is a process the
  // user cannot see and did not ask for.
  setTerminalProject(null);
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
  leaveHelp();
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
  if (embedded && !utility) {
    tellWorkspace("settings", { route: route || location.hash });
    if (isConfigRoute(location.hash)) history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  const app = document.getElementById("app");
  if (!app.classList.contains("showing-config")) {
    cameFrom = app.classList.contains("showing-home") ? "home" : "workspace";
  }
  leaveHelp();
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

function leaveHelp() {
  const app = document.getElementById("app");
  if (!app.classList.contains("showing-help")) return;
  app.classList.remove("showing-help");
  if (isHelpRoute(location.hash)) {
    history.replaceState(null, "", location.pathname + location.search);
  }
}

/** Same contract as showConfig(): never calls disconnect(), so reading the
 *  help while a turn is running does not cost the session. */
function showHelp(route) {
  if (embedded && !utility) {
    tellWorkspace("settings", { route: route || location.hash });
    if (isHelpRoute(location.hash)) history.replaceState(null, "", location.pathname + location.search);
    return;
  }
  const app = document.getElementById("app");
  if (!app.classList.contains("showing-help")) {
    cameFrom = app.classList.contains("showing-home") ? "home" : "workspace";
  }
  leaveConfig();
  app.classList.add("showing-help");
  document.title = "QuickCode — Help";
  if (route && location.hash !== route) { location.hash = route; return; }
  renderHelp();
}

function closeHelp() {
  if (utility) { tellWorkspace("utility-close"); return; }
  leaveHelp();
  if (cameFrom === "home") { showHome(); return; }
  showWorkspace();
  document.title = store.bootstrap?.project
    ? `QuickCode — ${store.bootstrap.project}` : "QuickCode";
}

function closeConfig() {
  if (utility) { tellWorkspace("utility-close"); return; }
  if (cameFrom === "home") { showHome(); return; }
  showWorkspace();
  document.title = store.bootstrap?.project
    ? `QuickCode — ${store.bootstrap.project}` : "QuickCode";
}

// ---- the update chip ----
//
// The calmest affordance the requirement allows: a chip that is simply not
// there unless there is a newer release. It never appears while the check is
// in flight, never appears when the check fails (a dead network must not
// produce a banner every launch — the Install page is where a failure is
// visible), and never does anything but link to the page that explains it.
// Fire-and-forget from boot, so nothing waits on it and no turn is touched.

const UPDATES_ROUTE = "#/config/install/updates";

async function refreshUpdateChip() {
  const chip = $("update-chip");
  if (!chip) return;
  let status;
  try {
    status = await api.update();
  } catch {
    return;                       // silent by design
  }
  if (!status?.update_available) return;
  chip.textContent = `↑ ${status.latest}`;
  chip.title = `QuickCode ${status.latest} is available — you are running ${
    status.installed}. Click for the release notes and what updating means here.`;
  chip.classList.remove("hidden");
}

// ---- project + conversation lifecycle ----

async function openProject(project, { resume = null, seq = null } = {}) {
  setProject(project.id);
  setPanelProject(project.id);
  setTerminalProject(project.id);
  rememberProject(project.id);
  showWorkspace();

  $("project-chip").textContent = project.name || "…";
  clearSessionBar();

  let bs;
  try {
    bs = await api.bootstrap();
  } catch (err) {
    showHome();
    $("home-projects").insertAdjacentHTML("afterbegin",
      `<div class="home-err">Could not open that project (${esc(err.message)}).</div>`);
    return;
  }
  store.bootstrap = bs;
  applyTheme(bs.theme);
  tellWorkspace("theme", { theme: bs.theme });
  const chip = $("project-chip");
  chip.textContent = bs.project + (bs.git_branch ? ` · ${bs.git_branch}` : "");
  chip.title = bs.cwd;
  $("model-pill").textContent = shortModel(bs.default_model) + " ▾";
  document.title = `QuickCode — ${bs.project}`;

  await openConversation(resume, { seq });

  // Opening a project can no longer start its MCP servers on its own. Ask what
  // was refused and put it in front of the user — a project that silently
  // loses its tools is the failure mode this whole gate has to avoid.
  checkTrust(project.id);
}

// An event to open the inspector on once its conversation has replayed: a
// search hit names a session *and* the message in it that matched.
let reveal = null;

function revealSeq(seq) {
  if (!Number.isSafeInteger(seq)) return;
  if (store.replaying || store.connection !== "open") reveal = { convId: store.convId, seq };
  else inspect(seq);
}

async function openConversation(resume, { seq = null } = {}) {
  const at = Number.isSafeInteger(seq) ? seq : null;
  if (resume && resume === store.convId) {
    if (at !== null) revealSeq(at);
    return;
  }
  if (embedded && store.convId) {
    tellWorkspace("open-session", { convId: resume || null, ...(at !== null ? { seq: at } : {}) });
    return;
  }
  const pid = currentProject();
  clearQueue();
  try {
    const { conv_id } = await api.openConversation(resume || undefined);
    reveal = at !== null ? { convId: conv_id, seq: at } : null;
    connect(pid, conv_id);
  } catch (err) {
    $("transcript").innerHTML =
      `<div class="err-note">Could not start a conversation (${esc(err.message)}).</div>`;
    return;
  }
  refreshSessionBar();
}

// ---- boot ----

async function boot() {
  // Read before initAuth(), which drops the launch fragment: a reload of a
  // linked configuration page must land back on that page.
  const wantsConfig = isConfigRoute(location.hash);
  const wantsHelp = isHelpRoute(location.hash);
  const { token, project, resumeHint } = initAuth();
  if (embedded) {
    if (utility) document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !document.querySelector(".modal-backdrop, .menu")) {
        e.preventDefault(); e.stopImmediatePropagation(); tellWorkspace("utility-close");
      }
    }, true);
    window.addEventListener("pointerdown", () => tellWorkspace("focus"), true);
    window.addEventListener("focus", () => tellWorkspace("focus"));
    document.addEventListener("keydown", (e) => {
      if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      if (document.querySelector(".modal-backdrop, .menu, dialog[open]")) return;
      if (!["n", "z", "b", "arrowleft", "arrowright", "arrowup", "arrowdown"].includes(e.key.toLowerCase())) return;
      e.preventDefault(); e.stopImmediatePropagation();
      tellWorkspace("shortcut", { key: e.key, altKey: true });
    }, true);
    window.addEventListener("message", (e) => {
      if (e.origin !== location.origin || e.source !== window.parent || e.data?.source !== "qc-workspace") return;
      if (e.data.action === "focus") $("input")?.focus();
      if (e.data.action === "renamed") refreshSessionBar();
      if (e.data.action === "reveal") revealSeq(e.data.seq);
    });
  }

  // One inspector for every surface: chat, the agents panel and the
  // trajectory table all land on the same Summary/Payload/Result/Timing view.
  const openTrace = (seq) => {
    openPanelTab("trajectory");
    selectSeq(seq, { scroll: true });
  };
  setInspector(openTrace);
  initCopy();
  initChat({ openTrace });
  initTrajectory();
  initPanel();
  initTerminal();
  initReviews();
  initComposer({ onNewConversation: () => openConversation(null) });
  // The second status surface. The bar at the bottom is a dense readout you
  // consult; this one is a sign of life you cannot miss, next to the input.
  initActivity();
  // Freshly trusted MCP tools only reach a conversation that starts after the
  // grant, so the trust card can offer that restart itself.
  initTrust({ onNewConversation: () => openConversation(null) });
  initHome({ onOpen: (proj, opts) => openProject(proj, opts) });
  wireLogo($("brand-home").querySelector("img"), "brand-mark-fallback");
  wireLogo($("config-brand").querySelector("img"), "brand-mark-fallback");

  $("brand-home").addEventListener("click", showHome);
  // Configuration and help are install-wide, so Home carries them too.
  initConfig({ api, onDone: closeConfig });
  initHelp({ api, onDone: closeHelp });
  $("config-brand").addEventListener("click", closeConfig);
  $("help-brand").addEventListener("click", closeHelp);
  $("help-done").addEventListener("click", closeHelp);
  $("home-settings").addEventListener("click", () => showConfig(lastConfigRoute()));
  // The quick reference stays a modal -- recalling one shortcut mid-sentence
  // should not cost a view transition -- and offers the full view as a door.
  $("home-help").addEventListener("click", () =>
    openHelp({ onFull: () => showHelp(lastHelpRoute()) }));
  $("btn-new-chat").addEventListener("click", () => openConversation(null));
  $("btn-settings").addEventListener("click", () => showConfig(lastConfigRoute()));
  $("btn-quick-settings").addEventListener("click", () =>
    openQuickSettings({ onFull: () => showConfig(DEFAULT_ROUTE) }));
  $("update-chip").addEventListener("click", () => showConfig(UPDATES_ROUTE));

  // Every configuration page is a URL. A hash that names one shows the view;
  // anything else (including "back" out of it) returns to where we were.
  window.addEventListener("hashchange", () => {
    const app = document.getElementById("app");
    if (isConfigRoute(location.hash)) showConfig();
    else if (isHelpRoute(location.hash)) showHelp();
    else if (app.classList.contains("showing-config")) closeConfig();
    else if (app.classList.contains("showing-help")) closeHelp();
  });
  initSessionBar({
    embedded,
    onPick: (convId, { seq } = {}) => openConversation(convId, { seq }),
    onNew: () => openConversation(null),
    onTitle: (title) => reportPane(title),
  });

  subscribe((kind, ev) => {
    if (kind === "reset" || kind === "state") {
      pendingReviews.clear();
      for (const review of store.state?.pending || []) pendingReviews.add(review.req_id);
    }
    if (kind === "event" && ["permission_request", "plan_request"].includes(ev.type)) {
      if (!store.replaying || store.state?.pending?.some((p) => p.req_id === ev.req_id)) pendingReviews.add(ev.req_id);
      reportPane();
    }
    if (kind === "event" && ["permission_resolved", "plan_resolved"].includes(ev.type)) {
      pendingReviews.delete(ev.req_id); reportPane();
    }
    if (["state", "connection", "review", "replay_done"].includes(kind)) reportPane();
    if (kind === "replay_done" && reveal?.convId === store.convId) {
      const { seq } = reveal;
      reveal = null;
      inspect(seq);
    }
    // A switch changes what every configuration page would say about this
    // session, and the view caches the kernel for the life of a visit.
    if (kind === "event" && ev.type === "composition_changed") invalidateConfig();
    // Same for a posture: the rail draws the profile list with the active one
    // ticked, and that tick is now stale.
    if (kind === "event" && ev.type === "profile_changed") invalidateConfig();
  });
  initStatusBar();
  initConnBanner({ newSession: () => openConversation(null) });
  if (!utility) {
    initPaneNotices({
      tell: embedded ? (notice) => tellWorkspace("notice", notice) : null,
      label: () => $("session-chip").textContent.replace(/\s*▾$/, ""),
    });
  }

  if (!token) {
    showHome();
    $("home-projects").innerHTML =
      `<div class="home-err">No auth token. Open QuickCode through the URL printed
       by the CLI — it carries the token in the fragment.</div>`;
    return;
  }

  // Not awaited: boot must not wait on a network call, least of all this one.
  refreshUpdateChip();

  // Only a fragment-carried project skips Home.
  if (utility) {
    setProject(project);
    try { store.bootstrap = await api.bootstrap(); applyTheme(store.bootstrap.theme); } catch { /* settings shows its own error */ }
  } else if (project) {
    // A pane opened from a search hit carries the matching event's seq.
    const at = new URLSearchParams(location.search).get("at") || "";
    const seq = resumeHint && /^-?\d{1,15}$/.test(at) ? Number(at) : null;
    await openProject({ id: project }, { resume: resumeHint, seq });
  } else {
    showHome();
  }
  if (wantsConfig) showConfig();
  if (wantsHelp) showHelp();
}

boot();
