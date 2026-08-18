// Project trust — the visible half of the gate (docs/TRUST-HANDOFF.md).
//
// A project can name programs for QuickCode to run in two places: `mcpServers`
// in its `.quickcode/settings.json`, and `kind: tool` files in
// `.quickcode/plugins/`. Both spawn a command as the user, both are committed
// to the repository, and both are therefore inert until the project has been
// trusted once — under a single grant, because they are one risk wearing two
// hats. That refusal must not be silent: a project that quietly loses its
// tools is a worse bug than the hole the gate closes.
//
// This module owns the banner that names what was refused, shows the commands
// as they are written in the file, and takes the decision. It never guesses —
// everything it claims comes from GET .../trust, which carries each command
// tool's argv, and from the kernel's view of each `mcp.<name>` plugin, which
// carries that server's raw JSON block.

import { api } from "./api.js";
import { store } from "./store.js";
import { el, esc } from "./util.js";

/** Arm-then-act on the same button: the first click only changes the label and
 *  disarms itself after a moment, the second one is the decision. The idiom
 *  home.js uses for deletes; a security choice deserves it at least as much.
 *  Exported so there is one implementation, not two that drift. */
export function armed(btn, prompt, resting) {
  if (btn.dataset.armed === "1") return true;
  btn.dataset.armed = "1";
  btn.classList.add("armed");
  btn.textContent = prompt;
  setTimeout(() => {
    if (!btn.isConnected || btn.dataset.armed !== "1") return;
    delete btn.dataset.armed;
    btn.classList.remove("armed");
    btn.textContent = resting;
  }, 4000);
  return false;
}

function disarm(btn, resting) {
  delete btn.dataset.armed;
  btn.classList.remove("armed");
  btn.textContent = resting;
}

// ---- what the user last approved, so a re-prompt can say what moved ----
//
// The backend binds trust to a hash, not to a list, so the report cannot say
// which server was added. It is worth saying, so the browser keeps its own
// note of the block it approved. It is a convenience, never an authority:
// when it is missing the copy says so instead of inventing a comparison.

const SEEN_KEY = (pid) => `qc-trust-approved:${pid || "-"}`;

function readApproved(pid) {
  try {
    const raw = localStorage.getItem(SEEN_KEY(pid));
    const data = raw ? JSON.parse(raw) : null;
    return data && typeof data.servers === "object" ? data : null;
  } catch { return null; }
}

function writeApproved(pid, status, specs) {
  const servers = {};
  for (const name of status.servers || []) servers[name] = commandLine(specs.get(name));
  try {
    localStorage.setItem(SEEN_KEY(pid), JSON.stringify({ hash: status.hash, servers }));
  } catch { /* quota — the diff is a nicety, the decision is not */ }
}

function forgetApproved(pid) {
  try { localStorage.removeItem(SEEN_KEY(pid)); } catch { /* nothing to undo */ }
}

/** added / removed / changed, relative to the block this browser approved. */
function diffApproved(pid, status, specs) {
  const prev = readApproved(pid);
  if (!prev) return null;
  const now = new Map((status.servers || []).map((n) => [n, commandLine(specs.get(n))]));
  const added = [...now.keys()].filter((n) => !(n in prev.servers));
  const removed = Object.keys(prev.servers).filter((n) => !now.has(n));
  const changed = [...now.keys()].filter(
    (n) => n in prev.servers && prev.servers[n] !== now.get(n));
  return added.length || removed.length || changed.length
    ? { added, removed, changed } : null;
}

// ---- reading the actual mcpServers block ----

function commandLine(spec) {
  if (!spec) return "";
  if (spec.line) return spec.line;
  const parts = [spec.command, ...(spec.args || [])].filter(Boolean);
  return parts.join(" ");
}

// The kernel registers every declared MCP server as plugin `mcp.<name>`, whose
// view is that server's definition verbatim. Reading it here is how the prompt
// can show a command instead of asking for consent to a name.
async function loadSpecs(names) {
  const out = new Map();
  await Promise.all((names || []).map(async (name) => {
    let entry = { command: "", args: [], line: "", raw: null };
    try {
      const plugin = await api.plugin(`mcp.${name}`);
      let raw = null;
      try { raw = JSON.parse(plugin?.view?.content ?? ""); } catch { raw = null; }
      entry = {
        command: plugin?.metadata?.command || "",
        args: Array.isArray(plugin?.metadata?.args) ? plugin.metadata.args : [],
        line: plugin?.description || "",
        raw,
      };
    } catch { /* the row says the command could not be read */ }
    out.set(name, entry);
  }));
  return out;
}

function rawBlock(names, specs) {
  const servers = {};
  for (const name of names || []) {
    const spec = specs.get(name);
    servers[name] = spec?.raw ?? { command: spec?.command || "", args: spec?.args || [] };
  }
  return JSON.stringify({ mcpServers: servers }, null, 2);
}

// ---- state ----

let hooks = { onNewConversation: () => {} };
let host = null;        // the strip under the top bar that holds the card
let chip = null;        // the persistent top-bar affordance
let current = null;     // { status, specs, pid }
let collapsed = false;

function ensureMounts() {
  if (!host) {
    const workspace = document.getElementById("view-workspace");
    const topbar = document.getElementById("topbar");
    if (!workspace || !topbar) return false;
    host = el(`<div id="trust-host" class="trust-host" aria-live="polite"></div>`);
    topbar.insertAdjacentElement("afterend", host);
  }
  if (!chip) {
    const actions = document.querySelector("#topbar .topbar-actions");
    if (!actions) return false;
    chip = el(`<button id="trust-chip" class="trust-chip hidden"></button>`);
    chip.addEventListener("click", () => {
      collapsed = !collapsed;
      // Reopening shows where the project stands, not the receipt for the last
      // thing that was done to it.
      if (!collapsed && current) { current.granted = null; current.revoked = false; }
      render();
    });
    actions.insertAdjacentElement("afterbegin", chip);
  }
  return true;
}

// ---- copy ----
//
// Written for someone who is not a security specialist and is one click from a
// subprocess. It says what trusting does, in the plain sense of "runs this
// command on your computer", and stops there.

const NOTE_BOUND =
  "Trust is recorded for this folder and for the configuration shown here. If " +
  "the mcpServers block or a command tool is edited later, QuickCode asks " +
  "again before running it.";

const NOTE_SESSION =
  "The chat that is open now keeps the tool set it started with. Start a new " +
  "chat to use these tools.";

const NOTE_REVOKE =
  "Revoking stops QuickCode from starting these again — on the next open, and " +
  "for anything that would start one after that. A server process that is " +
  "already running keeps running until this project is closed; a command tool " +
  "stops being offered to the agent in new chats.";

function serverWord(n) { return n === 1 ? "server" : "servers"; }
function toolWord(n) { return n === 1 ? "command tool" : "command tools"; }

// A project may declare MCP servers, authored command tools, or both, and the
// two gate together under one grant. Every sentence that counts what is being
// approved goes through here so none of them can name only half of it.
function declared(status) {
  const servers = (status.servers || []).length;
  const tools = (status.tools || []).length;
  const parts = [];
  if (servers) parts.push(`${servers} MCP ${serverWord(servers)}`);
  if (tools) parts.push(`${tools} ${toolWord(tools)}`);
  return { servers, tools, total: servers + tools, phrase: parts.join(" and ") };
}

// What a refused command tool would run. The backend reads the file for us —
// an untrusted tool is not registered, so there is nothing else left to show,
// and a name on its own is not something anyone can consent to.
function toolList(status) {
  const detail = status.tool_detail || [];
  if (!detail.length) return "";
  const rows = detail.map((t) => `<li class="trust-srv">
      <span class="ts-name">${esc(t.name || t.file || "?")}</span>
      <code class="ts-cmd">${(t.argv || []).length
        ? esc(t.argv.join(" "))
        : "this file declares no command that could be read"}</code>
      ${t.file ? `<span class="ts-env">.quickcode/plugins/${esc(t.file)}</span>` : ""}
    </li>`).join("");
  return `<ul class="trust-srvs">${rows}</ul>`;
}

// ---- rendering ----

function settingsPath() {
  const cwd = store.bootstrap?.cwd;
  return cwd ? `${cwd} → .quickcode/settings.json` : ".quickcode/settings.json";
}

function serverList(status, specs) {
  const rows = (status.servers || []).map((name) => {
    const spec = specs.get(name);
    const line = commandLine(spec);
    const env = spec?.raw && spec.raw.env && typeof spec.raw.env === "object"
      ? Object.keys(spec.raw.env) : [];
    return `<li class="trust-srv">
      <span class="ts-name">${esc(name)}</span>
      <code class="ts-cmd">${line
        ? esc(line)
        : "the command could not be read from the kernel"}</code>
      ${env.length ? `<span class="ts-env">sets ${env.length} environment
        variable${env.length === 1 ? "" : "s"}: ${esc(env.join(", "))}</span>` : ""}
    </li>`;
  }).join("");
  return `<ul class="trust-srvs">${rows}</ul>`;
}

function rawDetails(status, specs) {
  return `<details class="trust-raw">
    <summary>Show the mcpServers block as it is written</summary>
    <div class="trust-raw-path">${esc(settingsPath())}</div>
    <pre>${esc(rawBlock(status.servers, specs))}</pre>
  </details>`;
}

function changedList(diff) {
  const part = (label, names) => names.length
    ? `<li><span class="tc-label">${label}</span>
        <code>${esc(names.join(", "))}</code></li>` : "";
  return `<ul class="trust-changed">
    ${part("added", diff.added)}
    ${part("command changed", diff.changed)}
    ${part("removed", diff.removed)}
  </ul>`;
}

function card() {
  const { status, specs, pid } = current;
  const d = declared(status);
  const n = d.servers;
  // `reason` is the backend's own distinction between "never trusted" and
  // "trusted, then the config was edited" — the two cases need different copy,
  // and the report is the only place that knows which one this is.
  const changedSinceGrant = status.inert && /changed/.test(status.reason || "");

  if (!status.inert) return trustedCard();

  const diff = changedSinceGrant ? diffApproved(pid, status, specs) : null;
  const title = changedSinceGrant
    ? "This project's configuration changed"
    : d.total === 1
      ? "This project wants to run a command on your machine"
      : `This project wants to run ${d.total} commands on your machine`;

  // A server started under the previous grant is still up: the gate governs
  // future starts, it does not kill a running process. Saying "nothing is
  // running" while one is would be the wrong kind of reassuring.
  const stillUp = (status.running || []).filter((s) => (status.servers || []).includes(s));

  const intro = changedSinceGrant
    ? `<p>You trusted this project before. What it asks to run is no longer what
        you approved, so QuickCode has not started ${
  d.total === 1 ? "it" : "them"} under the new configuration.
        Read the commands again before approving them: an edit can add a server
        or a command tool, or change what an existing one runs.</p>
       ${diff ? changedList(diff) : `<p class="trust-muted">This browser has no
        record of the configuration you approved, so it cannot show a
        comparison. Read the block below as if for the first time.</p>`}
       ${stillUp.length ? `<p class="trust-muted">${esc(stillUp.join(", "))}
        ${stillUp.length === 1 ? "is" : "are"} still running from the
        configuration you approved earlier. Revoking or leaving this off does
        not stop ${stillUp.length === 1 ? "it" : "them"}; ${
  stillUp.length === 1 ? "it stops" : "they stop"} when this project is
        closed.</p>` : ""}`
    : `<p>This project declares ${d.phrase} in its own files. QuickCode has not
        started ${d.total === 1 ? "it" : "them"}. ${d.servers
  ? "Starting an MCP server runs its command on this computer, as you, with "
        + "access to your files and your network."
  : ""} ${d.tools
  ? "A command tool is a program this project lets the agent run, defined in a "
        + "file the project itself commits."
  : ""}</p>
       <p>Read the commands before you decide. If this project came from
        somewhere you do not control, judge them the way you would judge any
        script you were handed.</p>`;

  const el_ = el(`<section class="trust-card warn" role="region"
      aria-label="Project trust decision">
    <div class="trust-head">
      <span class="trust-icon">△</span>
      <h2 class="trust-title">${esc(title)}</h2>
      <button class="trust-collapse" title="Collapse this to the top bar">▴</button>
    </div>
    <div class="trust-body">
      ${intro}
      ${d.servers ? serverList(status, specs) : ""}
      ${d.servers ? rawDetails(status, specs) : ""}
      ${toolList(status)}
      <p class="trust-note">${esc(NOTE_BOUND)}</p>
      <p class="trust-err hidden"></p>
    </div>
    <div class="trust-foot">
      <button class="btn primary trust-grant">${
        changedSinceGrant ? "Approve the new configuration" : "Trust this project"}</button>
      <button class="btn trust-later">Leave them off</button>
    </div>
  </section>`);

  const grant = el_.querySelector(".trust-grant");
  const resting = grant.textContent;
  grant.addEventListener("click", async () => {
    if (!armed(grant, "Confirm: run these commands", resting)) return;
    grant.textContent = "…";
    grant.disabled = true;
    try {
      const next = await api.grantTrust();
      writeApproved(pid, next, specs);
      current = { ...current, status: next, granted: next.connected || [] };
      collapsed = false;
      render();
    } catch (err) {
      grant.disabled = false;
      disarm(grant, resting);
      fail(el_, `Could not record trust: ${err.message}`);
    }
  });
  el_.querySelector(".trust-later").addEventListener("click", () => {
    collapsed = true; render();
  });
  el_.querySelector(".trust-collapse").addEventListener("click", () => {
    collapsed = true; render();
  });
  return el_;
}

function trustedCard() {
  const { status, specs, granted, wasRunning } = current;
  const names = status.servers || [];
  // POST answers with `connected`, not `running`: what is up after a grant is
  // what was already up plus what the grant just started.
  const known = status.running
    ?? [...new Set([...(wasRunning || []), ...(granted || [])])];
  const running = known.filter((s) => names.includes(s));
  const silent = granted ? names.filter((s) => !running.includes(s)) : [];

  const head = granted
    ? (granted.length
      ? `Trusted. ${granted.length} ${serverWord(granted.length)} started: ${
        granted.join(", ")}.`
      : "Trusted. No new server started.")
    : "This project is trusted";

  const el_ = el(`<section class="trust-card ok" role="region"
      aria-label="Project trust">
    <div class="trust-head">
      <span class="trust-icon">✓</span>
      <h2 class="trust-title">${esc(head)}</h2>
      <button class="trust-collapse" title="Collapse this to the top bar">▴</button>
    </div>
    <div class="trust-body">
      <p>QuickCode may run the ${declared(status).phrase || "commands"} declared
        in this project's files, which means running the
        ${declared(status).total === 1 ? "command" : "commands"} listed below on
        this computer. ${esc(NOTE_BOUND)}</p>
      ${silent.length ? `<p class="trust-muted">${esc(silent.join(", "))} did not
        start. A server whose command does not launch is skipped and the reason
        is written to the QuickCode log; the trust decision itself was
        recorded.</p>` : ""}
      ${granted ? `<p class="trust-note">${esc(NOTE_SESSION)}</p>` : ""}
      ${names.length ? serverList(status, specs) : ""}
      ${names.length ? `<p class="trust-muted">Running in this project now: ${
        running.length ? esc(running.join(", ")) : "none"}.</p>` : ""}
      ${names.length ? rawDetails(status, specs) : ""}
      ${toolList(status)}
      <p class="trust-note">${esc(NOTE_REVOKE)}</p>
      <p class="trust-err hidden"></p>
    </div>
    <div class="trust-foot">
      ${granted ? `<button class="btn primary trust-newchat">Start a new chat</button>` : ""}
      <button class="btn danger trust-revoke">Revoke trust</button>
      <button class="btn trust-later">Close</button>
    </div>
  </section>`);

  const revoke = el_.querySelector(".trust-revoke");
  revoke.addEventListener("click", async () => {
    if (!armed(revoke, "Confirm: revoke", "Revoke trust")) return;
    revoke.textContent = "…";
    revoke.disabled = true;
    try {
      const next = await api.revokeTrust();
      // There is no approved block any more, so there is nothing to diff a
      // future prompt against.
      forgetApproved(current.pid);
      current = { ...current, status: next, granted: null, revoked: true };
      collapsed = false;
      render();
    } catch (err) {
      revoke.disabled = false;
      disarm(revoke, "Revoke trust");
      fail(el_, `Could not revoke trust: ${err.message}`);
    }
  });
  el_.querySelector(".trust-newchat")?.addEventListener("click", () => {
    collapsed = true;
    render();
    hooks.onNewConversation();
  });
  el_.querySelector(".trust-later").addEventListener("click", () => {
    collapsed = true; render();
  });
  return el_;
}

function fail(card_, message) {
  const err = card_.querySelector(".trust-err");
  err.textContent = message;
  err.classList.remove("hidden");
}

function render() {
  if (!ensureMounts()) return;
  host.innerHTML = "";
  if (!current) { chip.classList.add("hidden"); return; }
  const { status, error } = current;

  if (error) {
    chip.classList.remove("hidden");
    chip.className = "trust-chip unknown";
    chip.textContent = "MCP trust unknown";
    chip.title = `QuickCode could not read this project's trust status (${error}).`;
    return;
  }
  const d = declared(status);
  if (!d.total) { chip.classList.add("hidden"); return; }

  const n = d.total;
  chip.classList.remove("hidden");
  if (status.inert) {
    chip.className = "trust-chip warn";
    chip.textContent = `△ ${d.phrase} not started`;
    chip.title = `This project declares ${d.phrase} that will not run because `
      + "the project is not trusted. Click to review them.";
  } else {
    chip.className = "trust-chip ok";
    chip.textContent = `Trusted (${n})`;
    chip.title = `This project is trusted to run ${d.phrase}. `
      + "Click to review or revoke.";
  }
  if (current.revoked && !collapsed) {
    const done = el(`<section class="trust-card warn" role="region"
        aria-label="Project trust">
      <div class="trust-head">
        <span class="trust-icon">△</span>
        <h2 class="trust-title">Trust revoked</h2>
      </div>
      <div class="trust-body">
        <p>QuickCode will not run ${d.phrase} in this project again. A server
          process that is already running keeps running until this project is
          closed.</p>
      </div>
      <div class="trust-foot"><button class="btn trust-later">Close</button></div>
    </section>`);
    done.querySelector(".trust-later").addEventListener("click", () => {
      collapsed = true; render();
    });
    host.appendChild(done);
    return;
  }
  if (collapsed) return;
  host.appendChild(card());
}

// ---- public ----

export function initTrust(h) {
  hooks = { onNewConversation: () => {}, ...(h || {}) };
}

/** Forget the current project's banner. Called when the workspace is left, so
 *  the next project is asked about fresh rather than inheriting an answer. */
export function resetTrust() {
  current = null;
  collapsed = false;
  if (host) host.innerHTML = "";
  if (chip) chip.classList.add("hidden");
}

/** Report the trust decision for the project the shell has just opened, and
 *  raise the banner when servers were refused. Safe to call more than once. */
export async function checkTrust(pid) {
  resetTrust();
  if (!ensureMounts()) return null;
  let status;
  try {
    status = await api.trust();
  } catch (err) {
    current = { error: err.message, pid };
    render();
    return null;
  }
  const specs = status.has_servers ? await loadSpecs(status.servers) : new Map();
  current = { status, specs, pid, wasRunning: status.running || [] };
  collapsed = !status.inert;   // a trusted project says its piece in the chip
  render();
  return status;
}

/** The one-line summary a Home card shows for a project it has opened. */
export async function trustSummary(pid) {
  try { return await api.trustOf(pid); } catch { return null; }
}
