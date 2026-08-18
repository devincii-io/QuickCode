// The tool picker. It edits **patterns**, and that is the whole design.
//
// A checkbox grid writes explicit names. Tick `mcp__docs__*` in a grid and what
// gets written is a snapshot of the tools that server happened to expose at
// that instant; the next tool it adds never reaches the agent, and nothing in
// the UI ever says so. So this picker never rewrites a glob into names behind
// the user's back. Clicking an ungranted row appends its literal name; clicking
// a row a glob claimed offers to remove the glob, naming it, rather than
// quietly expanding it.
//
// Four row states, not two:
//
//   matched          a literal pattern names this tool
//   matched-by-glob  a glob claimed it — the glob is shown in the ← column
//   unmatched        in the pool, not granted, with the reason
//   excluded         not grantable at all (the delegation pair is granted by
//                    depth, `plan` never reaches a subagent). Offering these a
//                    checkbox would offer a promise the runtime breaks.
//
// Every state is server-computed, from the same resolver the runner uses, so
// the rows and the preview can never disagree.

import { esc } from "../util.js";
import { sheet, splitError } from "../settings/ui.js";

const STATE_MARK = {
  "matched": "✓",
  "matched-by-glob": "≈",
  "unmatched": "○",
  "excluded": "⊘",
};

const STATE_LABEL = {
  "matched": "granted by name",
  "matched-by-glob": "granted by a pattern",
  "unmatched": "not granted",
  "excluded": "not grantable",
};

// Any family over this many rows arrives collapsed: "grant all" on an MCP
// server must write one glob, not 24 names.
const BULK_THRESHOLD = 6;

function groupRows(rows) {
  const groups = new Map();
  for (const row of rows) {
    if (!groups.has(row.group)) groups.set(row.group, []);
    groups.get(row.group).push(row);
  }
  return [...groups.entries()];
}

function rowHtml(row) {
  const mark = STATE_MARK[row.state] || "○";
  const via = row.state === "excluded"
    ? `<span class="tp-why">${esc(row.reason)}</span>`
    : row.pattern
      ? `<code class="tp-pattern">${esc(row.pattern)}</code>`
      : `<span class="tp-why">${esc(row.reason || "")}</span>`;
  return `<button class="tp-row" data-name="${esc(row.name)}"
      data-state="${esc(row.state)}" data-pattern="${esc(row.pattern || "")}"
      ${row.state === "excluded" ? "disabled" : ""}
      title="${esc(STATE_LABEL[row.state] || "")}">
    <span class="tp-mark">${mark}</span>
    <code class="tp-name">${esc(row.name)}</code>
    <span class="tp-desc">${esc(row.description || "")}</span>
    <span class="tp-flag ${row.read_only ? "ro" : "rw"}">${row.read_only ? "R" : "W"}</span>
    <span class="tp-flag sh">${row.shell ? "shell" : ""}</span>
    <span class="tp-via">${via}</span>
  </button>`;
}

function familyHtml(family, rows) {
  const granted = rows.filter((r) => r.state.startsWith("matched")).length;
  const ro = rows.filter((r) => r.read_only).length;
  return `<details class="tp-family">
    <summary>
      <span class="tp-mark">${granted === rows.length ? "✓" : granted ? "≈" : "○"}</span>
      <code class="tp-name">${esc(family)}</code>
      <span class="tp-desc">${rows.length} tools, ${ro} read-only ·
        ${granted} granted</span>
      <span class="tp-via"><button class="ghost-btn" data-grant-family="${esc(family)}"
        >grant all</button></span>
    </summary>
    ${rows.map(rowHtml).join("")}
  </details>`;
}

/**
 * Open the picker over an agent.
 *
 * `state` is `{ patterns, inherits, editable, editableReason }`; `onChange`
 * receives `{patterns, inherits}` on every edit so the caller can re-preview,
 * and `onApply` is called once with the final value.
 */
export function openToolPicker({ agent, data, onChange, onApply }) {
  let patterns = [...(data.grant?.patterns || [])];
  let inherits = !!data.grant?.inherits;
  let rows = data.pool || [];
  let footer = data.footer || "";
  const editable = data.grant?.editable !== false;

  const node = sheet(
    `Tools for <code>${esc(agent)}</code>`,
    `<div class="tp">
       <div class="tp-grant">
         <div class="tp-grant-head">
           <span class="tp-grant-label">Grant patterns</span>
           <span class="tp-matched"></span>
         </div>
         <div class="tp-chips"></div>
         <div class="tp-add">
           <input class="tp-input" placeholder="a name or a glob — read, mcp__docs__*"
                  spellcheck="false">
           <button class="ghost-btn" data-add>+ pattern</button>
         </div>
         <label class="tp-inherit">
           <input type="checkbox" ${inherits ? "checked" : ""} data-inherit>
           inherit everything the composition allows (<code>tools: null</code>)
         </label>
       </div>
       ${editable ? "" : `<div class="tp-locked">${esc(data.grant?.editable_reason
         || "This composition cannot be edited here.")}</div>`}
       <div class="tp-filter">
         <input class="tp-search" placeholder="filter…" spellcheck="false">
         <span class="tp-legend">
           <span>✓ by name</span><span>≈ by pattern</span>
           <span>○ not granted</span><span>⊘ not grantable</span>
         </span>
       </div>
       <div class="tp-list"></div>
     </div>`,
    `<span class="tp-footer"></span>
     <span class="tp-foot-actions">
       <button class="btn" data-cancel>Cancel</button>
       <button class="btn primary" data-apply ${editable ? "" : "disabled"}>Apply</button>
     </span>`,
    { wide: true },
  );

  const listEl = node.querySelector(".tp-list");
  const chipsEl = node.querySelector(".tp-chips");
  const footEl = node.querySelector(".tp-footer");
  const matchedEl = node.querySelector(".tp-matched");
  const searchEl = node.querySelector(".tp-search");
  let filter = "";

  function paintChips() {
    chipsEl.innerHTML = inherits
      ? `<span class="tp-inherit-note">every tool the spawning agent holds —
           patterns are ignored while this is on</span>`
      : (patterns.length
        ? patterns.map((p, i) => `<span class="tp-chip">
            <code>${esc(p)}</code>
            <button class="tp-x" data-drop="${i}" title="remove this pattern">×</button>
          </span>`).join("")
        : `<span class="tp-inherit-note">no patterns — this agent gets no
             tools at all</span>`);
    const granted = rows.filter((r) => r.state.startsWith("matched")).length;
    matchedEl.textContent = `matched ${granted} of ${rows.length} tools`;
  }

  function paintRows() {
    const q = filter.trim().toLowerCase();
    const visible = rows.filter(
      (r) => !q || r.name.toLowerCase().includes(q)
        || (r.description || "").toLowerCase().includes(q));
    listEl.innerHTML = groupRows(visible).map(([group, groupRows_]) => {
      const families = new Map();
      const loose = [];
      for (const r of groupRows_) {
        if (!r.family) { loose.push(r); continue; }
        if (!families.has(r.family)) families.set(r.family, []);
        families.get(r.family).push(r);
      }
      const chunks = [];
      for (const [family, members] of families) {
        chunks.push(members.length > BULK_THRESHOLD || !q
          ? familyHtml(family, members)
          : members.map(rowHtml).join(""));
      }
      chunks.push(loose.map(rowHtml).join(""));
      return `<div class="tp-group">
        <div class="tp-group-head"><span>${esc(group)}</span>
          <span class="tp-group-count">${groupRows_.length}</span></div>
        ${chunks.join("")}
      </div>`;
    }).join("") || `<div class="set-empty">Nothing matches “${esc(filter)}”.</div>`;
    footEl.textContent = footer;
  }

  function repaint() { paintChips(); paintRows(); }

  async function changed() {
    repaint();
    if (!onChange) return;
    try {
      const next = await onChange({ patterns: [...patterns], inherits });
      if (next) {
        rows = next.pool || rows;
        footer = next.footer || footer;
        repaint();
      }
    } catch (err) {
      footEl.textContent = splitError(err).detail;
    }
  }

  function addPattern(p) {
    const text = (p || "").trim();
    if (!text || patterns.includes(text)) return;
    patterns.push(text);
    inherits = false;
    node.querySelector("[data-inherit]").checked = false;
    changed();
  }

  listEl.addEventListener("click", (e) => {
    const family = e.target.closest("[data-grant-family]");
    if (family) {
      e.preventDefault();
      // The glob, never the 24 names it currently matches.
      addPattern(family.dataset.grantFamily);
      return;
    }
    const row = e.target.closest(".tp-row");
    if (!row || row.disabled) return;
    e.preventDefault();
    const { name, state, pattern } = row.dataset;
    if (state === "unmatched") { addPattern(name); return; }
    if (state === "matched") {
      patterns = patterns.filter((p) => p !== pattern && p !== name);
      changed();
      return;
    }
    if (state === "matched-by-glob") {
      // Expanding the glob here is the one thing the picker must not do
      // silently, so the choice is stated in full and the glob is named.
      const ok = window.confirm(
        `“${name}” is granted by the pattern “${pattern}”.\n\n`
        + `Removing that pattern removes every tool it matches. Negative `
        + `patterns (excluding one tool from a glob) are not supported yet, and `
        + `expanding the glob into names would freeze the set — a tool the `
        + `server adds later would never reach this agent.\n\n`
        + `Remove “${pattern}”?`);
      if (!ok) return;
      patterns = patterns.filter((p) => p !== pattern);
      changed();
    }
  });

  chipsEl.addEventListener("click", (e) => {
    const x = e.target.closest("[data-drop]");
    if (!x) return;
    patterns.splice(Number(x.dataset.drop), 1);
    changed();
  });

  node.querySelector("[data-add]").addEventListener("click", () => {
    const input = node.querySelector(".tp-input");
    addPattern(input.value);
    input.value = "";
    input.focus();
  });
  node.querySelector(".tp-input").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    addPattern(e.currentTarget.value);
    e.currentTarget.value = "";
  });
  node.querySelector("[data-inherit]").addEventListener("change", (e) => {
    inherits = e.currentTarget.checked;
    changed();
  });
  searchEl.addEventListener("input", (e) => { filter = e.currentTarget.value; paintRows(); });
  node.querySelector("[data-cancel]").addEventListener("click", () => node.closeSheet());
  node.querySelector("[data-apply]").addEventListener("click", async () => {
    try {
      await onApply?.({ patterns: [...patterns], inherits });
      node.closeSheet();
    } catch (err) {
      footEl.textContent = splitError(err).detail;
      footEl.classList.add("is-err");
    }
  });

  repaint();
  return node;
}
