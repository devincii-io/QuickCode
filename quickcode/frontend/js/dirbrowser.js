// The folder picker behind "Open folder…".

import { api } from "./api.js";
import { closeModal, modal } from "./ui/modal.js";
import { esc } from "./util.js";

/** Folder picker for "Open folder…". Directory-only by design — the backing
 *  endpoint never reports files. `onPick` receives an absolute path. */
export function openDirBrowser(onPick) {
  const m = modal(
    "Open folder",
    `<div class="dirb">
       <div class="dirb-bar">
         <button class="ghost-btn dirb-up" title="Parent directory">↑</button>
         <input class="dirb-path" spellcheck="false" placeholder="Paste or type a path…">
         <button class="btn dirb-go">Go</button>
       </div>
       <div class="dirb-list"></div>
       <div class="dirb-msg"></div>
     </div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary dirb-select">Open this folder</button>`,
  );
  const pathInput = m.querySelector(".dirb-path");
  const listEl = m.querySelector(".dirb-list");
  const msgEl = m.querySelector(".dirb-msg");
  const upBtn = m.querySelector(".dirb-up");
  let here = null;
  let parent = null;

  async function go(path) {
    msgEl.textContent = "";
    listEl.innerHTML = `<div class="dirb-empty">loading…</div>`;
    let data;
    try {
      data = await api.dir(path);
    } catch (err) {
      listEl.innerHTML = "";
      msgEl.textContent = err.message;
      return;
    }
    here = data.path;
    parent = data.parent;
    pathInput.value = here;
    upBtn.disabled = !parent;
    listEl.innerHTML = data.dirs.length
      ? data.dirs.map((d) => `
          <button class="dirb-row" data-path="${esc(d.path)}">
            <span class="dirb-icon">${d.is_git ? "⎇" : "▸"}</span>
            <span class="dirb-name">${esc(d.name)}</span>
            ${d.is_git ? '<span class="dirb-git">git</span>' : ""}
          </button>`).join("")
      : `<div class="dirb-empty">No sub-folders here.</div>`;
  }

  listEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-path]");
    if (b) go(b.dataset.path);
  });
  upBtn.addEventListener("click", () => { if (parent) go(parent); });
  m.querySelector(".dirb-go").addEventListener("click", () => go(pathInput.value.trim()));
  pathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(pathInput.value.trim()); }
  });
  m.querySelector(".dirb-select").addEventListener("click", async () => {
    const chosen = pathInput.value.trim() || here;
    if (!chosen) return;
    msgEl.textContent = "opening…";
    try {
      const project = await api.openProject(chosen);
      closeModal();
      onPick(project);
    } catch (err) {
      msgEl.textContent = err.message;
    }
  });

  go(null);
  return m;
}
