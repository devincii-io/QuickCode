// Deleting QuickCode's data for a project.

import { api } from "./api.js";
import { reportBulk } from "./selection.js";
import { closeModal, modal } from "./ui/modal.js";
import { esc } from "./util.js";

/** The one destructive action here that gets a real dialog rather than the
 *  arm-then-act button.
 *
 *  Removing a project from the list is reversible by reopening the folder, so
 *  a two-click button is proportionate. This is not: it unlinks transcripts,
 *  task boards and artifacts that exist nowhere else. So it names the exact
 *  directory it will remove, lists what is inside it, and says out loud the
 *  thing the user actually needs to know — that the code is not part of it.
 *
 *  `projects` is `[{id, name, path}]`; the same dialog covers a bulk purge. */
export function openPurgeProjects(projects, { onDone } = {}) {
  const many = projects.length > 1;
  const m = modal(
    many ? `Delete QuickCode data for ${projects.length} projects` : "Delete QuickCode data",
    `<div class="purge">
       <div class="purge-lead">This removes the <code>.quickcode</code> folder inside
         ${many ? "each project" : "the project"} and forgets
         ${many ? "their" : "its"} trust decision. It cannot be undone.</div>
       <div class="purge-list"><div class="hs-note">reading…</div></div>
       <div class="purge-safe">Your files are not touched. Nothing outside
         <code>.quickcode</code> is removed, and the project folder itself stays
         exactly where it is.</div>
       <div class="rn-err"></div>
     </div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn danger" data-purge disabled>Delete the data</button>`,
  );
  const list = m.querySelector(".purge-list");
  const err = m.querySelector(".rn-err");
  const go = m.querySelector("[data-purge]");

  const line = (p, d) => {
    const bits = [];
    if (d?.sessions) bits.push(`${d.sessions} session${d.sessions === 1 ? "" : "s"}`);
    if (d?.archived) bits.push(`${d.archived} archived`);
    if (d?.boards) bits.push(`${d.boards} task board${d.boards === 1 ? "" : "s"}`);
    if (d?.artifacts) bits.push(`${d.artifacts} artifact${d.artifacts === 1 ? "" : "s"}`);
    const what = !d
      ? "could not read this project's data directory"
      : d.exists
        ? (bits.length ? bits.join(" · ") : "settings and cached state only")
        : "nothing stored here yet";
    return `<div class="purge-item">
      <div class="purge-name">${esc(p.name || p.path)}</div>
      <div class="purge-path"><code>${esc(d?.path || p.path)}</code></div>
      <div class="purge-what">${esc(what)}</div>
    </div>`;
  };

  (async () => {
    const summaries = await Promise.all(projects.map(async (p) => {
      try { return await api.projectData(p.id); } catch { return null; }
    }));
    list.innerHTML = projects.map((p, i) => line(p, summaries[i])).join("");
    go.disabled = false;
  })();

  go.addEventListener("click", async () => {
    go.disabled = true;
    go.textContent = "Deleting…";
    err.textContent = "";
    let result;
    try {
      result = many
        ? await api.purgeProjects(projects.map((p) => p.id))
        : { removed: [await api.purgeProjectData(projects[0].id)], skipped: [] };
    } catch (e) {
      go.disabled = false;
      go.textContent = "Delete the data";
      err.textContent = e.message;
      return;
    }
    closeModal();
    reportBulk(result.removed.length, result.skipped,
      { one: "project", many: "projects", verb: "Deleted the QuickCode data of" });
    if (onDone) onDone(result);
  });
  return m;
}
