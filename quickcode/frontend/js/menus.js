// The mode and model menus behind the composer's pills, and the custom model
// id dialog the model menu ends in.

import { api } from "./api.js";
import { MODES } from "./modes.js";
import { store } from "./store.js";
import { menuAt } from "./ui/menu.js";
import { closeModal, modal } from "./ui/modal.js";
import { el, esc, fmtTokens } from "./util.js";
import { actions } from "./ws.js";

export function openModeMenu(anchor) {
  const cur = store.state?.mode;
  const allowYolo = store.bootstrap?.allow_yolo;
  const items = MODES
    .filter(([id]) => id !== "yolo" || allowYolo)
    .map(([id, title, desc]) => `<button class="menu-item" data-mode="${id}">
      <div class="mi-title">${title}${cur === id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-desc">${desc}</div></button>`).join("");
  const m = menuAt(anchor, items);
  m.addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (b) { actions.setMode(b.dataset.mode); m.closeMenu(); }
  });
}

export async function openModelMenu(anchor) {
  let models = [];
  try { models = await api.models(); } catch { /* offline */ }
  const cur = store.state?.model;
  // The whole catalog, not a slice of it: the search box is what makes 400
  // entries navigable, and a cap would hide the model somebody came for.
  const render = (list) => list.map((mo) => `
    <button class="menu-item" data-model="${esc(mo.id)}" title="${esc(mo.id)}">
      <div class="mi-title"><span class="mi-name">${esc(mo.name || mo.id)}</span>${
        cur === mo.id ? '<span class="check">✓</span>' : ""}</div>
      <div class="mi-meta">${esc(mo.id)} · ctx ${fmtTokens(mo.context_length)}${
        mo.prompt_price != null ? ` · $${mo.prompt_price}/M in` : ""}</div>
    </button>`).join("") || `<div class="menu-note">No models match.</div>`;
  const m = menuAt(anchor, render(models), { searchable: true });
  const list = m.querySelector(".menu-list");
  const search = m.querySelector(".menu-search");
  // A pinned footer, so the escape hatch stays reachable without scrolling
  // past the whole catalog.
  const foot = el(`<div class="menu-foot">
    <button class="menu-item" data-custom>
      <div class="mi-title">Custom model id…</div>
      <div class="mi-desc">Use any id the provider accepts, listed or not.</div>
    </button></div>`);
  m.appendChild(foot);
  const customDesc = foot.querySelector(".mi-desc");
  search?.focus();
  search?.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    list.innerHTML = render(models.filter((mo) =>
      (mo.id + " " + (mo.name || "")).toLowerCase().includes(q)));
    list.scrollTop = 0;
    customDesc.textContent = q
      ? `Use “${search.value.trim()}” as the model id.`
      : "Use any id the provider accepts, listed or not.";
  });
  m.addEventListener("click", (e) => {
    if (e.target.closest("[data-custom]")) {
      const typed = search?.value.trim() || "";
      m.closeMenu();
      askCustomModel(typed);
      return;
    }
    const b = e.target.closest("[data-model]");
    if (b) { actions.setModel(b.dataset.model); m.closeMenu(); }
  });
}

/** Free-text model id. The backend takes any string; an id the catalog does
 *  not know simply comes back with no context length until the provider says. */
function askCustomModel(prefill = "") {
  const m = modal(
    "Custom model id",
    `<div style="font-size:13px;color:var(--fg-dim);margin-bottom:10px">
       The catalog is a convenience, not a gate — anything your provider accepts
       works here. An id it does not list keeps the context meter blank.</div>
     <input class="deny-input" id="custom-model" spellcheck="false"
            placeholder="e.g. vendor/model-name" value="${esc(prefill)}">`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary" data-use>Use this model</button>`,
  );
  const input = m.querySelector("#custom-model");
  input.focus();
  input.select();
  const use = () => {
    const v = input.value.trim();
    if (!v) { input.focus(); return; }
    actions.setModel(v);
    closeModal();
  };
  m.querySelector("[data-use]").addEventListener("click", use);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); use(); }
  });
}
