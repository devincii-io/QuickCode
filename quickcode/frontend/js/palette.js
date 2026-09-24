// The command palette (Ctrl+K / ⌘K): everything you can do from where you
// are, in one searchable list.
//
// This module is the list and its keyboard; what is in it is the caller's.
// An agent pane offers its own commands (js/palette_pane.js), the workspace
// shell its own (js/workspace_palette.js), and each binds the key in its own
// document — a keystroke goes to whichever of the two has focus, so the
// palette that opens is the one for what you were doing.
//
// An item: { title, group, hint?, hintHtml?, keys?, keywords?, current?, run }.
// `hintHtml` is trusted markup built by the caller (a highlighted snippet).

import { highlightHtml, queryTerms } from "./text_match.js";
import { menuAt } from "./ui/menu.js";
import { debounce, esc } from "./util.js";

// The Settings pages worth jumping to. Agents, compositions and the rest have
// pages per item, which Settings' own search already covers.
export const SETTINGS_PAGES = [
  ["#/config/install/general", "Provider & defaults"],
  ["#/config/install/appearance", "Appearance"],
  ["#/config/install/models", "Models"],
  ["#/config/install/search", "Web search"],
  ["#/config/install/updates", "Updates"],
  ["#/config/agents", "Agents"],
  ["#/config/compositions", "Compositions"],
  ["#/config/profiles", "Permission profiles"],
  ["#/config/hooks", "Hooks"],
  ["#/config/parts/tools", "Parts"],
  ["#/config/machine-room", "Machine room"],
  ["#/config/problems", "Problems"],
];

const WORD_START = /[\s/:(.·—–-]/;

function subsequence(text, term) {
  let at = 0;
  for (const ch of term) {
    at = text.indexOf(ch, at);
    if (at === -1) return false;
    at += 1;
  }
  return true;
}

/** How well `item` matches every term; null when one of them is missing.
 *  The title counts most, the start of it or of a word in it most of all. */
export function scoreItem(item, terms) {
  const title = String(item.title).toLowerCase();
  const rest = `${item.hint || ""} ${item.group || ""} ${item.keywords || ""}`.toLowerCase();
  let score = 0;
  for (const term of terms) {
    const at = title.indexOf(term);
    if (at === 0) score += 100;
    else if (at > 0 && WORD_START.test(title[at - 1])) score += 60;
    else if (at > 0) score += 30;
    else if (rest.includes(term)) score += 10;
    else if (terms.length === 1 && term.length > 1 && subsequence(title, term)) score += 5;
    else return null;
  }
  return score;
}

/** The items that match `query`, best first; ties keep their given order. */
export function rankItems(items, query) {
  const terms = queryTerms(query);
  if (!terms.length) return items.slice();
  return items
    .map((item, i) => ({ item, i, score: scoreItem(item, terms) }))
    .filter((r) => r.score !== null)
    .sort((a, b) => b.score - a.score || a.i - b.i)
    .map((r) => r.item);
}

/** Groups in the order they first appear, for the unfiltered list. */
export function groupItems(items) {
  const groups = new Map();
  for (const item of items) {
    const name = item.group || "";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(item);
  }
  return [...groups];
}

let serial = 0;

/** Open the palette. `items` show at once; `load` (a promise of more items)
 *  joins them when it settles; `search(query)` returns extra items for a
 *  query — the session search — drawn under the ranked ones. */
export function openPalette({ items, load = null, search = null, searchGroup = "",
  placeholder = "Type a command or search…" }) {
  const back = document.activeElement;
  const id = `qc-palette-${++serial}`;
  let running = false;
  const m = menuAt(null, "", {
    searchable: true, center: true, className: "palette",
    // Dismissed: focus goes back where it was.
    onClose: () => { if (!running && back?.isConnected) back.focus?.(); },
  });
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-modal", "true");
  m.setAttribute("aria-label", "Command palette");
  const input = m.querySelector(".menu-search");
  const list = m.querySelector(".menu-list");
  input.placeholder = placeholder;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-controls", `${id}-list`);
  input.setAttribute("aria-autocomplete", "list");
  input.spellcheck = false;
  list.id = `${id}-list`;
  list.setAttribute("role", "listbox");
  list.setAttribute("aria-label", "Commands");
  const status = document.createElement("div");
  status.className = "sr-only";
  status.setAttribute("aria-live", "polite");
  const foot = document.createElement("div");
  foot.className = "palette-foot";
  foot.textContent = "↑↓ select · Enter run · Esc close";
  m.append(foot, status);

  let all = items.slice();
  let extra = [];
  let shown = [];
  let active = 0;
  let asked = 0;

  const option = (item, i, terms) => `
    <div class="menu-item pal-item" role="option" id="${id}-o${i}" data-i="${i}"
         aria-selected="${i === active}">
      <div class="mi-title"><span class="mi-name">${highlightHtml(item.title, terms)}</span>${
        item.current ? '<span class="check" aria-label="current">✓</span>' : ""}${
        item.keys ? `<kbd class="pal-keys">${esc(item.keys)}</kbd>` : ""}${
        terms.length && item.group ? `<span class="mi-tag">${esc(item.group)}</span>` : ""}</div>
      ${item.hintHtml ? `<div class="mi-desc">${item.hintHtml}</div>`
        : item.hint ? `<div class="mi-desc">${esc(item.hint)}</div>` : ""}
    </div>`;

  const section = (name, html, n) => `<div role="group" aria-labelledby="${id}-g${n}">
    <div class="menu-head" id="${id}-g${n}">${esc(name)}</div>${html}</div>`;

  function draw() {
    const terms = queryTerms(input.value);
    const ranked = rankItems(all, input.value);
    shown = ranked.concat(extra);
    active = Math.min(active, Math.max(0, shown.length - 1));
    let html = "";
    let n = 0;
    if (terms.length) {
      html = ranked.map((item, i) => option(item, i, terms)).join("");
    } else {
      let i = 0;
      for (const [name, group] of groupItems(ranked)) {
        html += section(name, group.map((item) => option(item, i++, terms)).join(""), n++);
      }
    }
    if (extra.length) {
      html += section(searchGroup, extra.map((item, j) => option(item, ranked.length + j, [])).join(""), n++);
    }
    list.innerHTML = html || '<div class="menu-note">Nothing matches.</div>';
    status.textContent = shown.length ? `${shown.length} result${shown.length === 1 ? "" : "s"}` : "No results";
    mark();
  }

  function mark({ scroll = true } = {}) {
    for (const node of list.querySelectorAll('[aria-selected="true"]')) node.setAttribute("aria-selected", "false");
    const node = list.querySelector(`[data-i="${active}"]`);
    if (!node) { input.removeAttribute("aria-activedescendant"); return; }
    node.setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", node.id);
    if (scroll) node.scrollIntoView({ block: "nearest" });
  }

  // After a command, focus returns only if the command left it nowhere — and
  // never into a pane's frame, which would tell the shell that pane was
  // chosen, undoing a command that just chose another. `leaves` marks one
  // that hands off to the other document.
  function run(i) {
    const item = shown[i];
    if (!item) return;
    running = true;
    m.closeMenu();
    item.run();
    const now = document.activeElement;
    if (!item.leaves && (!now || now === document.body) && back?.isConnected && back.tagName !== "IFRAME") {
      back.focus?.();
    }
  }

  const lookup = debounce(async (q) => {
    const mine = ++asked;
    if (!search || queryTerms(q).join("").length < 2) {
      if (extra.length) { extra = []; draw(); }
      return;
    }
    let found = [];
    try { found = await search(q); } catch { found = []; }
    if (mine !== asked || !m.isConnected) return;
    extra = found;
    draw();
  }, 250);

  input.addEventListener("input", () => {
    active = 0;
    asked++;
    extra = [];
    draw();
    lookup(input.value.trim());
  });
  input.addEventListener("keydown", (e) => {
    if (e.isComposing) return;
    const last = shown.length - 1;
    const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 8, PageUp: -8 }[e.key];
    if (step) {
      e.preventDefault();
      if (last < 0) return;
      active = Math.abs(step) === 1 ? (active + step + shown.length) % shown.length
        : Math.max(0, Math.min(last, active + step));
      mark();
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(active);
    } else if (e.key === "Tab") {
      e.preventDefault();   // the palette is modal: focus stays in its box
    } else if (e.key === "Escape") {
      // menuAt's own Escape handler is armed a tick late; one pressed at once
      // must still close the palette, and must not reach the composer, where
      // Escape interrupts the agent.
      e.preventDefault();
      e.stopPropagation();
      m.closeMenu();
    }
  });
  list.addEventListener("mousemove", (e) => {
    const node = e.target.closest("[data-i]");
    if (node && Number(node.dataset.i) !== active) { active = Number(node.dataset.i); mark({ scroll: false }); }
  });
  // mousedown, not click: the input must keep focus while the pointer is down.
  list.addEventListener("mousedown", (e) => e.preventDefault());
  list.addEventListener("click", (e) => {
    const node = e.target.closest("[data-i]");
    if (node) run(Number(node.dataset.i));
  });

  draw();
  input.focus();
  load?.then((more) => {
    if (!m.isConnected || !more?.length) return;
    all = all.concat(more);
    draw();
  }).catch(() => { /* the list without them is still a list */ });
  return m;
}

/** ⌘K on a Mac, Ctrl+K elsewhere. Only the one: on macOS Ctrl+K is the text
 *  fields' own "delete to the end of the line". */
export function isPaletteKey(e, mac) {
  if (String(e.key).toLowerCase() !== "k" || e.altKey || e.shiftKey) return false;
  return mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
}

/** The palette key in this document opens `open()`, or closes a palette
 *  already showing. Nothing happens under a modal dialog or a Settings sheet,
 *  both of which sit above the palette. */
export function bindPaletteKey(open) {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  document.addEventListener("keydown", (e) => {
    if (!isPaletteKey(e, mac)) return;
    if (document.querySelector(".modal-backdrop, .set-sheet, dialog[open]")) return;
    e.preventDefault();
    const showing = document.querySelector(".menu.palette");
    if (showing) { showing.closeMenu?.(); return; }
    open();
  });
}
