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
import { initComposer, refreshCompositionPill, refreshProfilePill } from "./composer.js";
import { initHome, refreshHome, rememberProject } from "./home.js";
import { initReviews, openHelp, openQuickSettings, openSessionMenu } from "./modals.js";
import {
  DEFAULT_ROUTE, initConfig, invalidate as invalidateConfig, isConfigRoute,
  lastConfigRoute, render as renderConfig,
} from "./config/view.js";
import {
  initHelp, isHelpRoute, lastHelpRoute, render as renderHelp,
} from "./help/view.js";
import { initPanel, openPanelTab, setPanelProject } from "./panel.js";
import { store, subscribe } from "./store.js";
import { setInspector } from "./inspect.js";
import { initTerminal, setTerminalProject } from "./terminal/panel.js";
import { checkTrust, initTrust, resetTrust } from "./trust.js";
import { initTrajectory, selectSeq } from "./trajectory.js";
import { toastOk } from "./toast.js";
import { applyTheme, debounce, esc, fmtCost, fmtMs, fmtTokens, oneLine, wireLogo } from "./util.js";
import { connect, connectionHealth, disconnect, retryNow } from "./ws.js";

const $ = (id) => document.getElementById(id);

// ---- view switching ----

// Which view was showing before Configuration took over, so "Done" goes back
// to what the user was doing rather than to an arbitrary home.
let cameFrom = "home";

function showHome() {
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
  leaveHelp();
  if (cameFrom === "home") { showHome(); return; }
  showWorkspace();
  document.title = store.bootstrap?.project
    ? `QuickCode — ${store.bootstrap.project}` : "QuickCode";
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

// ---- the connection banner ----
//
// `store.connection` used to drive one dot in the status bar. A dot is what
// you consult once you are already suspicious; it is not what tells you.
// Meanwhile everything else on screen kept looking live — the status segment
// still said "streaming" because status events had simply stopped arriving,
// and Stop still offered to interrupt a turn it could no longer reach.
//
// The banner is graded, because "the socket closed" covers two very different
// events. A 1013 overflow close is the server *deliberately* making us
// reconnect and replay; it is over in milliseconds and announcing it would be
// crying wolf several times an hour. A server that has stopped is the
// opposite: nothing the user types can ever arrive, and in the native window
// there is no address bar to reload from. So: silence for the first moment, a
// calm line after that, and an unmistakable one — with a button — once it has
// been down long enough to be a real outage.

const CONN_QUIET_MS = 1200;    // below this, a reconnect is not news
const CONN_LOUD_MS = 12000;    // above this, it is not a blip

let connTimer = null;
let connWasLoud = false;       // an outage the user was actually told about

function connCard({ tone, title, body, action }) {
  return `<div class="conn-card ${tone}" role="${tone === "warn" ? "alert" : "status"}">
    <span class="conn-dot"></span>
    <div class="conn-text">
      <span class="conn-title">${esc(title)}</span>
      <span class="conn-body">${esc(body)}</span>
    </div>
    ${action ? `<button class="btn conn-act" data-act="${action.id}">${
      esc(action.label)}</button>` : ""}
  </div>`;
}

// What to say, given how long it has been down and whether trying again could
// ever work. Split out so the wording is readable as prose in one place.
function connState(h, down) {
  if (h.fatal === "gone") {
    return {
      tone: "warn", title: "This session is no longer on the server",
      body: "QuickCode answered, and the answer was that it does not have this "
        + "conversation any more — the project may have been closed or the "
        + "session file removed. Nothing further will arrive here.",
      action: { id: "new", label: "Start a new session" },
    };
  }
  if (h.fatal === "denied") {
    return {
      tone: "warn", title: "QuickCode refused this connection",
      body: "The token this page is holding is no longer accepted. Open "
        + "QuickCode again from the URL the CLI printed — it carries a fresh one.",
      action: { id: "retry", label: "Try again" },
    };
  }
  if (down >= CONN_LOUD_MS) {
    // A socket that was never once live is a different problem from one that
    // dropped, and the difference is worth saying: the first points at the
    // launch (a stale token, the wrong port), the second at the server.
    return h.everOpen
      ? {
        tone: "warn", title: "QuickCode is not responding",
        body: `Nothing has reached this window for ${Math.round(down / 1000)}s. `
          + "The server may have stopped. Anything you send now will not "
          + "arrive; the window keeps trying in the background.",
        action: { id: "retry", label: "Try again now" },
      }
      : {
        tone: "warn", title: "Could not reach QuickCode",
        body: "This window has never managed to open a connection. The server "
          + "may not be running, or this page may be holding a token it no "
          + "longer accepts — reopening the URL the CLI printed fixes the second.",
        action: { id: "retry", label: "Try again now" },
      };
  }
  return {
    tone: "calm", title: "Reconnecting to QuickCode…",
    body: "The transcript reloads from the server's log as soon as it is back.",
    action: null,
  };
}

function renderConn() {
  const host = $("conn-host");
  if (!host) return;
  if (connTimer) { clearTimeout(connTimer); connTimer = null; }
  const h = connectionHealth();

  // Nothing to have an opinion about: no conversation is attached (Home), the
  // socket is up, or it is the first connect and has not failed yet — a
  // "connecting" that has never closed is just boot.
  if (!h.attached || (h.state === "open" && !h.fatal) || (!h.downSince && !h.fatal)) {
    if (!h.attached) connWasLoud = false;   // no outage to give a receipt for
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }

  const down = performance.now() - h.downSince;
  if (!h.fatal && down < CONN_QUIET_MS) {
    host.classList.add("hidden");
    host.innerHTML = "";
    // Come back and look again the moment the quiet period is over — nothing
    // else will fire while the socket sits closed.
    connTimer = setTimeout(renderConn, CONN_QUIET_MS - down);
    return;
  }

  connWasLoud = true;
  host.innerHTML = connCard(connState(h, down));
  host.classList.remove("hidden");
  host.querySelector("[data-act]")?.addEventListener("click", (e) => {
    if (e.currentTarget.dataset.act === "new") {
      // A fresh session is not a recovered one; it must not collect the
      // "reconnected, back in step" receipt for a transcript it never had.
      connWasLoud = false;
      openConversation(null);
    } else {
      retryNow();
    }
  });
  // The escalation is time-based, so it needs a wake-up of its own; past it,
  // the counter in the copy is worth refreshing at a human pace.
  if (!h.fatal) {
    connTimer = setTimeout(renderConn, down < CONN_LOUD_MS ? CONN_LOUD_MS - down : 5000);
  }
}

// ---- the session bar: the chip, and the tab strip beside it ----
//
// The chip shows the open session's own title — derived from the first user
// message unless it has been renamed — and opens the full list. The strip is
// the recently-used conversations as tabs, so switching is one click and you
// can see what else is there.
//
// The strip is emphatically a list of *shortcuts*. js/ws.js allows exactly one
// live socket and connect() resets the event store, so an inactive tab is an
// id and nothing more: clicking it leaves the conversation you are in. The
// titles say so, and no tab ever shows a running indicator.

const TAB_LIMIT = 6;          // beyond this the overflow button opens the list

// A session with nothing in it yet is listed as "(empty)", which is the right
// word for a row in a list of many and the wrong one for the session you are
// sitting in. Here it is the one you just started.
function label(s, n = 90) {
  const title = s?.title && s.title !== "(empty)" ? s.title : "New session";
  return oneLine(title, n);
}

function tabHint(s, active) {
  if (active) return `This session: ${label(s)}`;
  return `Switch to “${label(s)}” — QuickCode runs one conversation at a time, `
    + "so this leaves the one you are in.";
}

function renderSessionTabs(sessions, convId, mine) {
  const strip = $("session-tabs");
  if (!strip) return;
  // The archive is deliberately absent: a filed-away session is not a recent
  // one, and it is one click away in the popover, which does list it.
  const listed = sessions.filter((s) => !s.archived);
  const shown = listed.slice(0, TAB_LIMIT);
  // Whatever else has been touched more recently, the open session is a tab:
  // a switcher that cannot show you where you are is not a switcher. A brand
  // new conversation may not be in the list yet, so it gets a placeholder.
  if (convId && !shown.some((s) => s.conv_id === convId)) {
    if (shown.length >= TAB_LIMIT) shown.pop();
    shown.push(mine || { conv_id: convId, title: "New session" });
  }
  const hidden = Math.max(0, listed.length - shown.length);
  const tab = (s) => {
    const active = s.conv_id === convId;
    return `<button class="session-tab${active ? " active" : ""}"
      data-conv="${esc(s.conv_id)}" ${active ? 'aria-current="page"' : ""}
      title="${esc(tabHint(s, active))}">${esc(label(s, 22))}</button>`;
  };
  strip.innerHTML = shown.map(tab).join("") + (hidden
    ? `<button class="session-tab stab-more" data-more
         title="${hidden} more session${hidden === 1 ? "" : "s"} — the whole list, with
                rename, archive and delete">+${hidden}</button>`
    : "");
  // One tab and nothing hidden is the chip said twice; the strip stays away.
  strip.classList.toggle("hidden", shown.length < 2 && !hidden);
}

async function refreshSessionBar() {
  const chip = $("session-chip");
  const convId = store.convId;
  let sessions = [];
  try {
    sessions = await api.sessions();
  } catch { /* offline: keep whatever is on screen */ }
  if (store.convId !== convId) return;   // switched while we were asking
  const mine = sessions.find((s) => s.conv_id === convId);
  chip.textContent = label(mine, 42) + " ▾";
  chip.title = `Session ${convId} — click for the full list`;
  renderSessionTabs(sessions, convId, mine);
}

const bumpSessionBar = debounce(refreshSessionBar, 800);

// One set of hooks for both ways into the switcher — the chip's popover and
// the strip's overflow button — so they can never come to mean two things.
const switcher = {
  onPick: (convId) => openConversation(convId),
  onNew: () => openConversation(null),
};

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

async function openProject(project, { resume = null } = {}) {
  setProject(project.id);
  setPanelProject(project.id);
  setTerminalProject(project.id);
  rememberProject(project.id);
  showWorkspace();

  $("project-chip").textContent = project.name || "…";
  $("session-chip").textContent = "New session ▾";
  // The tabs belong to the project being left; blank them rather than let the
  // wrong project's sessions sit there until the new list arrives.
  $("session-tabs").innerHTML = "";
  $("session-tabs").classList.add("hidden");

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
  refreshSessionBar();
}

// ---- boot ----

async function boot() {
  // Read before initAuth(), which drops the launch fragment: a reload of a
  // linked configuration page must land back on that page.
  const wantsConfig = isConfigRoute(location.hash);
  const wantsHelp = isHelpRoute(location.hash);
  const { token, project, resumeHint } = initAuth();

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
  $("session-chip").addEventListener("click", (e) =>
    openSessionMenu(e.currentTarget, switcher));

  // The strip switches; it does not manage. Everything else a session can have
  // done to it lives one click away, in the popover the overflow button opens.
  $("session-tabs").addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (more) { openSessionMenu(more, switcher); return; }
    const tab = e.target.closest("[data-conv]");
    if (!tab || tab.dataset.conv === store.convId) return;
    openConversation(tab.dataset.conv);
  });

  // A rename can happen in the popover or in a Home row, and either may name
  // the session that is open; archiving and deleting happen in the popover and
  // change which sessions there are to switch to. Either way the bar re-reads
  // rather than guessing what the list now looks like.
  for (const ev of ["qc:session-renamed", "qc:sessions-changed"]) {
    window.addEventListener(ev, () => {
      if (!document.getElementById("app").classList.contains("showing-home")) {
        refreshSessionBar();
      }
    });
  }

  subscribe((kind, ev) => {
    if (kind === "state") refreshState();
    if (kind === "status") { refreshStatus(ev.state); refreshCompositionPill(); }
    if (kind === "queued") { queuedTexts.push(ev.text); renderQueue(); }
    if (kind === "event" && ev.type === "user_message") bumpSessionBar();
    // A switch changes what every configuration page would say about this
    // session, and the view caches the kernel for the life of a visit.
    if (kind === "event" && ev.type === "composition_changed") invalidateConfig();
    // Same for a posture: the rail draws the profile list with the active one
    // ticked, and that tick is now stale.
    if (kind === "event" && ev.type === "profile_changed") invalidateConfig();
    // Counts accumulate while a session replays, but the state event that
    // drives the status bar arrives before the replay does — without this the
    // bar reads "0 turns" over a fully rendered conversation.
    if (kind === "replay_done" || kind === "event") {
      if (store.state) refreshMetrics(store.state);
    }
    if (kind === "connection") {
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
      renderConn();
    }
    // Leaving a conversation (Home) detaches without a connection event, and a
    // banner about a socket nobody is waiting on is just noise.
    if (kind === "reset") renderConn();
    // Proof the session is genuinely back — an `open` socket has not replayed
    // anything yet, and may still be closed on us. Only an outage the user was
    // told about earns a receipt.
    if (kind === "replay_done" && connWasLoud) {
      connWasLoud = false;
      toastOk("Reconnected — the transcript is back in step with the server.");
    }
  });

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
  if (project) {
    await openProject({ id: project }, { resume: resumeHint });
  } else {
    showHome();
  }
  if (wantsConfig) showConfig();
  if (wantsHelp) showHelp();
}

boot();
