// Boot: auth → bootstrap → conversation → WebSocket. Wires the top bar,
// composer, status bar, and view switching (chat / trajectory / split).

import { api, initAuth } from "./api.js";
import { initChat } from "./chat.js";
import { initComposer } from "./composer.js";
import { initReviews, openSessions, openSettings } from "./modals.js";
import { store, subscribe } from "./store.js";
import { initTrajectory, selectSeq } from "./trajectory.js";
import { fmtCost, fmtTokens } from "./util.js";
import { connect } from "./ws.js";

const $ = (id) => document.getElementById(id);

let view = "chat";

function setView(v) {
  view = v;
  $("main").className = "mode-" + v;
  document.querySelectorAll(".view-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === v));
  localStorage.setItem("qc-view", v);
}

function applyTheme(theme) {
  const map = {
    background: "--bg", surface: "--surface", panel: "--panel", boost: "--boost",
    foreground: "--fg", primary: "--primary", secondary: "--secondary",
    accent: "--accent", success: "--success", warning: "--warning", error: "--error",
  };
  for (const [k, cssVar] of Object.entries(map)) {
    if (theme?.[k]) document.documentElement.style.setProperty(cssVar, theme[k]);
  }
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

// ---- boot ----

async function boot() {
  const { token, resumeHint } = initAuth();
  initChat({ openTrace: (seq) => { if (view === "chat") setView("split"); selectSeq(seq, { scroll: true }); } });
  initTrajectory();
  initReviews();
  initComposer({ onNewConversation: () => openConversation(null) });

  document.querySelectorAll(".view-tab").forEach((t) =>
    t.addEventListener("click", () => setView(t.dataset.view)));
  setView(localStorage.getItem("qc-view") || "chat");

  $("btn-sessions").addEventListener("click", () =>
    openSessions((convId) => openConversation(convId)));
  $("btn-new-chat").addEventListener("click", () => openConversation(null));
  $("btn-settings").addEventListener("click", () => openSettings());

  subscribe((kind, ev) => {
    if (kind === "state") refreshState();
    if (kind === "status") refreshStatus(ev.state);
    if (kind === "connection") {
      const c = $("st-conn");
      c.classList.toggle("off", store.connection !== "open");
      c.title = "connection: " + store.connection;
    }
  });

  let bs;
  try {
    bs = await api.bootstrap();
  } catch (err) {
    document.getElementById("transcript").innerHTML =
      `<div class="err-note">Cannot reach the QuickCode backend (${err.message}).
       ${token ? "" : "Open QuickCode through the URL printed by the CLI — it carries the auth token."}</div>`;
    return;
  }
  store.bootstrap = bs;
  applyTheme(bs.theme);
  $("project-chip").textContent = bs.project + (bs.git_branch ? ` · ${bs.git_branch}` : "");
  $("model-pill").textContent = shortModel(bs.default_model) + " ▾";
  document.title = `QuickCode — ${bs.project}`;

  await openConversation(resumeHint);
}

async function openConversation(resume) {
  const { conv_id } = await api.openConversation(resume || undefined);
  connect(conv_id);
}

boot();
