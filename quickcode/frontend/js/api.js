// REST client. The auth token arrives in the launch URL fragment (never sent
// to the server as a URL); it is captured once, kept in memory + session
// storage (per-tab), and stripped from the visible URL.
//
// The backend is multi-project: everything that belongs to one project lives
// under /api/projects/{pid}/…. `setProject` names the project the shell is
// currently showing, and the scoped helpers below follow it. Before any
// project is selected (or for an embedder that only ever has one) they fall
// back to the unscoped routes, which address the launch directory.

let token = "";
let projectId = null;

export function initAuth() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const fromHash = hash.get("token");
  let project = null;
  let resumeHint = null;
  if (fromHash) {
    token = fromHash;
    sessionStorage.setItem("qc-token", token);
    // Both riders are read here because the fragment is about to be dropped.
    project = hash.get("project");
    resumeHint = hash.get("resume");
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    token = sessionStorage.getItem("qc-token") || "";
  }
  return { token, project, resumeHint };
}

export function authToken() { return token; }

export function setProject(pid) { projectId = pid || null; }

export function currentProject() { return projectId; }

// Project-scoped path, with the unscoped route as the no-project fallback.
function P(suffix) {
  return projectId
    ? `/api/projects/${encodeURIComponent(projectId)}${suffix}`
    : `/api${suffix}`;
}

async function req(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {
      "x-quickcode-token": token,
      ...(body !== undefined ? { "content-type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  // ---- current project ----
  bootstrap: () => req("GET", P("/bootstrap")),
  // `archived: true` folds the archive back into the list; every row carries
  // an `archived` flag either way.
  sessions: (archived = false) => req("GET", P(`/sessions?archived=${archived}`)),
  archiveSession: (convId, on = true) =>
    req("POST", P(`/sessions/${encodeURIComponent(convId)}/${on ? "archive" : "unarchive"}`)),
  removeSession: (convId) => req("DELETE", P(`/sessions/${encodeURIComponent(convId)}`)),
  // Bulk paths answer per row: {deleted, skipped:[{conv_id, reason}], …}.
  removeSessions: (convIds) => req("POST", P("/sessions/delete"), { conv_ids: convIds }),
  cleanupSessions: (dryRun = false) => req("POST", P("/sessions/cleanup"), { dry_run: dryRun }),
  openConversation: (resume) => req("POST", P("/conversations"), resume ? { resume } : {}),
  models: (refresh = false) => req("GET", P(`/models?refresh=${refresh}`)),
  plugins: () => req("GET", P("/plugins")),
  gitStatus: () => req("GET", P("/git/status")),
  gitDiff: (path) => req("GET", P(`/git/diff?path=${encodeURIComponent(path)}`)),

  // ---- plugin kernel ----
  // The whole inventory: plugins + their groups + the MCP servers + the
  // preset the next session will run on.
  kernel: () => req("GET", P("/kernel")),
  // One plugin, including its `view` — the raw truth behind it. Fetched on
  // demand because rendering every schema up front would be wasteful.
  plugin: (id) => req("GET", P(`/kernel/plugins/${encodeURIComponent(id)}`)),
  // `patch` is {enabled?, settings?, confirmed?}. A rejection is meaningful:
  // 403 = locked, 409 = the detail is the risk to put in front of the user.
  updatePlugin: (id, patch) => req("PUT", P(`/kernel/plugins/${encodeURIComponent(id)}`), patch),
  presets: () => req("GET", P("/presets")),
  setPreset: (preset) => req("PUT", P("/presets/active"), { preset }),
  // The composed system prompt with each section's byte range.
  prompt: () => req("GET", P("/prompt")),

  // ---- permission profiles ----
  // A named permission posture: a starting mode plus allow/ask/deny lists.
  // Every write answers with the whole list again, so a page never has to
  // reconstruct what the server now holds.
  profiles: () => req("GET", P("/profiles")),
  // {id, title, description, mode, allow, ask, deny, scope, shadow?}.
  // 409 = the id belongs to a built-in; `shadow: true` says that was meant.
  saveProfile: (body) => req("POST", P("/profiles"), body),
  deleteProfile: (id, scope = "user") =>
    req("DELETE", P(`/profiles/${encodeURIComponent(id)}?scope=${encodeURIComponent(scope)}`)),
  // `""` clears the selection. Unlike a composition switch this never waits for
  // a turn boundary — it takes effect on every live session immediately. The
  // one refusal is 409: selecting a profile that lets the agent act without
  // asking is gated on the project having been trusted.
  setActiveProfile: (id) => req("POST", P("/profiles/active"), { id }),

  // ---- the agent workbench ----
  // Every agent identity, `@orchestrator` first and first-class: an inventory
  // that lists the spawnable agents and omits the one you talk to answers the
  // wrong question.
  agents: () => req("GET", P("/kernel/agents")),
  // What this agent actually gets, with provenance on every value, the exact
  // composed prompt and the exact tool schemas. `conv` reads a running
  // session's frozen snapshot (and says so); without it the answer is live.
  resolvedAgent: (id, { preset = "", parent = "", conv = "" } = {}) => {
    const q = new URLSearchParams();
    if (preset) q.set("preset", preset);
    if (parent) q.set("parent", parent);
    if (conv) q.set("conv", conv);
    const tail = q.toString() ? `?${q}` : "";
    return req("GET", P(`/kernel/agents/${encodeURIComponent(id)}/resolved${tail}`));
  },
  // The same shape for an unsaved draft, composed server-side. The browser
  // never reconstructs a prompt: a reconstruction drifts, and a preview that
  // drifts is worse than none because it is believed.
  previewAgent: (id, draft) =>
    req("POST", P(`/kernel/agents/${encodeURIComponent(id)}/preview`), draft),
  // Save a composition edit. 409 = the composition is built in, with the
  // recourse in the detail.
  saveComposition: (id, body) =>
    req("PUT", P(`/kernel/agents/${encodeURIComponent(id)}/composition`), body),
  // "Customise this…": duplicate a composition into a project-scoped one.
  deriveComposition: (id, name) =>
    req("POST", P(`/kernel/compositions/${encodeURIComponent(id)}/derive`),
        name ? { name } : {}),
  // Session-scoped switching. 409 = refused, and the detail is the reason —
  // never queued, never applied silently once the agent goes idle.
  switchComposition: (convId, preset) =>
    req("POST", P(`/kernel/conversations/${encodeURIComponent(convId)}/composition`),
        { preset }),

  // ---- authored plugins: the files you own ----
  // Everything here writes a file under .quickcode/plugins/ (project) or
  // ~/.quickcode/plugins/ (user) and takes effect in *new* sessions — a
  // running session's composition is frozen at open. Every write response
  // carries `applies_to` saying so, and the UI quotes it rather than
  // inventing its own reassurance.
  authored: () => req("GET", P("/kernel/authored")),
  // {kind, name, scope, title?, text?} → a commented template on disk.
  createAuthored: (body) => req("POST", P("/kernel/authored"), body),
  // Validate a draft that is not on disk. Writes nothing.
  validateAuthored: (body) => req("POST", P("/kernel/authored/validate"), body),
  authoredSource: (id) =>
    req("GET", P(`/kernel/authored/${encodeURIComponent(id)}/source`)),
  // Never refuses: it writes, then validates, and the problems come back with
  // the 200. Saving something broken is allowed — the alternative is an editor
  // that will not let you stop typing halfway.
  saveAuthoredSource: (id, text) =>
    req("PUT", P(`/kernel/authored/${encodeURIComponent(id)}/source`), { text }),
  // A move to .trash/, not an unlink.
  deleteAuthored: (id) =>
    req("DELETE", P(`/kernel/authored/${encodeURIComponent(id)}`)),
  // Materialise an editable copy. 400 = refused, and the detail is the reason
  // plus the recourse — an internal tool is the case that hits this.
  duplicatePlugin: (id, body = {}) =>
    req("POST", P(`/kernel/plugins/${encodeURIComponent(id)}/duplicate`), body),
  // The same array `GET /kernel` carries, alone, for polling after a write.
  kernelProblems: () => req("GET", P("/kernel/problems")),

  // ---- project trust (the MCP gate) ----
  // A project's own mcpServers are inert until the project is trusted once,
  // because starting one runs its command on this machine. GET reports what was
  // refused, POST grants and connects, DELETE revokes future connects.
  trust: () => req("GET", P("/trust")),
  grantTrust: () => req("POST", P("/trust")),
  revokeTrust: () => req("DELETE", P("/trust")),
  trustOf: (pid) => req("GET", `/api/projects/${encodeURIComponent(pid)}/trust`),
  revokeTrustOf: (pid) =>
    req("DELETE", `/api/projects/${encodeURIComponent(pid)}/trust`),

  // ---- project registry (never scoped) ----
  projects: () => req("GET", "/api/projects"),
  openProject: (path) => req("POST", "/api/projects/open", { path }),
  dir: (path) => req("GET", "/api/dir" + (path ? `?path=${encodeURIComponent(path)}` : "")),

  // ---- another project than the current one (the Home view) ----
  sessionsOf: (pid, archived = false) =>
    req("GET", `/api/projects/${encodeURIComponent(pid)}/sessions?archived=${archived}`),
  deleteSession: (pid, convId) =>
    req("DELETE", `/api/projects/${encodeURIComponent(pid)}/sessions/${encodeURIComponent(convId)}`),
  archiveSessionOf: (pid, convId, on = true) =>
    req("POST", `/api/projects/${encodeURIComponent(pid)}/sessions/${
      encodeURIComponent(convId)}/${on ? "archive" : "unarchive"}`),
  cleanupSessionsOf: (pid, dryRun = false) =>
    req("POST", `/api/projects/${encodeURIComponent(pid)}/sessions/cleanup`, { dry_run: dryRun }),

  // ---- per install ----
  putConfig: (cfg) => req("PUT", "/api/config", cfg),
  putApiKey: (key) => req("POST", "/api/apikey", { key }),
  // A search provider's key. Its own route because /api/config writes plain
  // text to config.json and this goes to the encrypted store; write-only, like
  // the model key — nothing ever reads one back out to the browser.
  putSearchKey: (provider, key) => req("POST", "/api/search-key", { provider, key }),

  // ---- update checking (the one outbound request QuickCode makes) ----
  // GET asks github.com only when a check is due, and answers 200 even when
  // the network is dead — `state: "unknown"` with the reason in `error`. So a
  // caller never has to treat "offline" as a failure, and the chrome can stay
  // quiet about it while the Install page says it out loud.
  update: (force = false) => req("GET", `/api/update?force=${force}`),
  // The off switch. Written at user scope, so it follows the install rather
  // than the project that happened to be open.
  setUpdateCheck: (on) =>
    req("PUT", "/api/update/settings", { check_automatically: !!on }),
  // Downloads the installer and verifies it against the release's own
  // SHA256SUMS.txt. 409 means the digest did not match — and by then the
  // bytes have already been deleted. Nothing is executed here.
  downloadUpdate: () => req("POST", "/api/update/download"),
  // Runs it, on an explicit click, naming the exact digest that was shown
  // beside the button. The file is hashed again before it starts.
  installUpdate: (path, sha256) =>
    req("POST", "/api/update/install", { confirm: true, path, sha256 }),
};
