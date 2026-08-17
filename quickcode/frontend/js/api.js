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
  sessions: () => req("GET", P("/sessions")),
  openConversation: (resume) => req("POST", P("/conversations"), resume ? { resume } : {}),
  models: (refresh = false) => req("GET", P(`/models?refresh=${refresh}`)),
  plugins: () => req("GET", P("/plugins")),
  gitStatus: () => req("GET", P("/git/status")),
  gitDiff: (path) => req("GET", P(`/git/diff?path=${encodeURIComponent(path)}`)),

  // ---- project registry (never scoped) ----
  projects: () => req("GET", "/api/projects"),
  openProject: (path) => req("POST", "/api/projects/open", { path }),
  dir: (path) => req("GET", "/api/dir" + (path ? `?path=${encodeURIComponent(path)}` : "")),

  // ---- another project than the current one (the Home view) ----
  sessionsOf: (pid) => req("GET", `/api/projects/${encodeURIComponent(pid)}/sessions`),
  deleteSession: (pid, convId) =>
    req("DELETE", `/api/projects/${encodeURIComponent(pid)}/sessions/${encodeURIComponent(convId)}`),

  // ---- per install ----
  putConfig: (cfg) => req("PUT", "/api/config", cfg),
  putApiKey: (key) => req("POST", "/api/apikey", { key }),
};
