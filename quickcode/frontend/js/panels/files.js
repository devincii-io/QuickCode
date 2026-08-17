// Files panel: the working tree as git sees it — branch, changed files, and
// an inline diff per file. Read-only; it refreshes itself after any tool that
// can touch the disk.

import { api, currentProject } from "../api.js";
import { subscribe } from "../store.js";
import { debounce, el, esc } from "../util.js";

const WRITING_TOOLS = new Set(["write", "edit", "bash"]);
const STATUS_CLASS = { M: "st-mod", "??": "st-new", A: "st-new", D: "st-del" };

export const panel = {
  id: "files",
  title: "Files",
  icon: "±",
  init(container) {
    container.classList.add("panel-files");
    container.innerHTML = `
      <div class="pf-head">
        <span class="pf-branch">…</span>
        <button class="pf-refresh" title="Refresh">⟳</button>
      </div>
      <div class="pf-list"></div>`;

    const branchEl = container.querySelector(".pf-branch");
    const listEl = container.querySelector(".pf-list");
    const openPaths = new Set();   // paths whose diff is expanded

    async function refresh() {
      let data;
      try {
        data = await api.gitStatus();
      } catch (e) {
        branchEl.textContent = "git unavailable";
        listEl.innerHTML = `<div class="pf-empty">${esc(e.message)}</div>`;
        return;
      }
      render(data);
    }

    function render(data) {
      if (!data.is_repo) {
        branchEl.textContent = "not a git repository";
        listEl.innerHTML = `<div class="pf-empty">No repository here.</div>`;
        return;
      }
      branchEl.textContent = data.branch || "(detached)";
      const files = data.files || [];
      if (!files.length) {
        listEl.innerHTML = `<div class="pf-empty">Working tree clean.</div>`;
        return;
      }
      listEl.innerHTML = "";
      for (const f of files) listEl.appendChild(fileRow(f));
    }

    function fileRow(f) {
      const cls = STATUS_CLASS[f.status] || "st-other";
      const row = el(`<div class="pf-file">
        <div class="pf-row">
          <span class="pf-chip ${cls}">${esc(f.status)}</span>
          <span class="pf-path">${esc(f.path)}</span>
        </div>
        <div class="pf-diff"></div>
      </div>`);
      const body = row.querySelector(".pf-diff");
      if (openPaths.has(f.path)) {
        row.classList.add("open");
        loadDiff(f.path, body);
      }
      row.querySelector(".pf-row").addEventListener("click", () => {
        const open = row.classList.toggle("open");
        if (open) { openPaths.add(f.path); loadDiff(f.path, body); }
        else { openPaths.delete(f.path); body.innerHTML = ""; }
      });
      return row;
    }

    async function loadDiff(path, body) {
      body.innerHTML = `<div class="pf-empty">loading…</div>`;
      let data;
      try {
        data = await api.gitDiff(path);
      } catch (e) {
        body.innerHTML = `<div class="pf-empty">${esc(e.message)}</div>`;
        return;
      }
      if (!data.diff) {
        body.innerHTML = `<div class="pf-empty">No textual diff.</div>`;
        return;
      }
      body.innerHTML = `<pre class="pf-pre">${diffHtml(data.diff)}</pre>` +
        (data.truncated ? `<div class="pf-trunc">diff truncated</div>` : "");
    }

    refresh();
    container.querySelector(".pf-refresh").addEventListener("click", refresh);

    const bump = debounce(refresh, 400);
    let shownProject = currentProject();
    subscribe((kind, ev) => {
      // A finished replay means the socket is live — and possibly pointed at a
      // different project, in which case the tree on screen belongs somewhere
      // else entirely. Keyed off replay_done rather than reset on purpose: a
      // reset also fires for every failed reconnect attempt, and re-asking a
      // server that is not answering just piles up errors.
      if (kind === "replay_done") {
        const pid = currentProject();
        if (pid !== shownProject) { shownProject = pid; openPaths.clear(); }
        bump();
        return;
      }
      if (kind === "event" && ev.type === "tool_result" && WRITING_TOOLS.has(ev.name)) bump();
    });
  },
};

// Escape first, colorize second — never the other way round.
function diffHtml(text) {
  return text.split("\n").map((line) => {
    const safe = esc(line);
    if (line.startsWith("+++") || line.startsWith("---")) return `<span class="d-meta">${safe}</span>`;
    if (line.startsWith("@@")) return `<span class="d-hunk">${safe}</span>`;
    if (line.startsWith("+")) return `<span class="d-add">${safe}</span>`;
    if (line.startsWith("-")) return `<span class="d-del">${safe}</span>`;
    if (line.startsWith("diff ") || line.startsWith("index ")) return `<span class="d-meta">${safe}</span>`;
    return safe;
  }).join("\n");
}
