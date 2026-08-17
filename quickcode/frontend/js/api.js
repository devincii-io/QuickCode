// REST client. The auth token arrives in the launch URL fragment (never sent
// to the server as a URL); it is captured once, kept in memory + session
// storage (per-tab), and stripped from the visible URL.

let token = "";
let resumeHint = null;

export function initAuth() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const fromHash = hash.get("token");
  if (fromHash) {
    token = fromHash;
    sessionStorage.setItem("qc-token", token);
    resumeHint = hash.get("resume");
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    token = sessionStorage.getItem("qc-token") || "";
  }
  return { token, resumeHint };
}

export function authToken() { return token; }

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
  bootstrap: () => req("GET", "/api/bootstrap"),
  sessions: () => req("GET", "/api/sessions"),
  openConversation: (resume) => req("POST", "/api/conversations", resume ? { resume } : {}),
  models: (refresh = false) => req("GET", `/api/models?refresh=${refresh}`),
  plugins: () => req("GET", "/api/plugins"),
  gitStatus: () => req("GET", "/api/git/status"),
  gitDiff: (path) => req("GET", `/api/git/diff?path=${encodeURIComponent(path)}`),
  putConfig: (cfg) => req("PUT", "/api/config", cfg),
  putApiKey: (key) => req("POST", "/api/apikey", { key }),
};
