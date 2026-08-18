// The preview pane: the exact bytes an agent receives, and the exact schemas
// it is handed.
//
// Two rules govern everything here.
//
// **Nothing is composed in JavaScript.** The text and the offsets come from
// `/resolved` and `/preview`, which render through `prompts/sections.py` and
// `prompts/subagent.py` themselves. A reconstruction would drift, and a preview
// that drifts is worse than no preview because it is believed.
//
// **Absences are shown.** An empty region is invisible: a user who set
// `skip_project_instructions` and lost a block has no way to discover the flag
// caused it. So every section that did *not* render is listed with the reason,
// struck through, under the prompt it is missing from.

import { esc } from "../util.js";
import { highlightJson } from "../settings/ui.js";

const num = (n) => Number(n || 0).toLocaleString();

// Python's offsets count code points; a JS string counts UTF-16 units. Slicing
// the raw string would drift by one per astral character — and the prompt does
// contain them (the em dashes are fine, the arrows are not always).
function slice(chars, start, end) {
  return chars.slice(start, end).join("");
}

function blockHtml(chars, block, prev) {
  const gap = block.start > prev ? slice(chars, prev, block.start) : "";
  const body = slice(chars, block.start, block.end);
  const title = `${block.title} · ${num(block.end - block.start)} chars`
    + (block.provenance?.source ? ` · from ${block.provenance.source}` : "");
  return (gap ? `<span class="pv-gap">${esc(gap)}</span>` : "")
    + `<span class="pv-block${block.overridden ? " is-overridden" : ""}"
             data-id="${esc(block.id)}" title="${esc(title)}"
        ><span class="pv-block-tag">${esc(block.title)}</span>${esc(body)}</span>`;
}

function promptHtml(prompt) {
  const chars = [...(prompt.text || "")];
  const blocks = [...(prompt.blocks || [])].sort((a, b) => a.start - b.start);
  let out = "";
  let cursor = 0;
  for (const b of blocks) {
    out += blockHtml(chars, b, cursor);
    cursor = Math.max(cursor, b.end);
  }
  if (cursor < chars.length) out += `<span class="pv-gap">${esc(slice(chars, cursor, chars.length))}</span>`;
  return `<pre class="pv-prompt">${out || esc(prompt.text || "")}</pre>`;
}

function absencesHtml(prompt) {
  const rows = prompt.absences || [];
  if (!rows.length) return "";
  return `<div class="pv-absences">
    <h5>Not in this prompt</h5>
    ${rows.map((a) => `<div class="pv-absent">
      <span class="pv-absent-id">${esc(a.title || a.id)}</span>
      <span class="pv-absent-why">${esc(a.reason)}</span>
    </div>`).join("")}
  </div>`;
}

function toolsHtml(data) {
  const tools = data.tools || [];
  if (!tools.length) {
    return `<div class="pv-empty">No tools. The model is handed an empty tool
      array and can only answer in text.</div>`;
  }
  return `<div class="pv-tools">${tools.map((t) => {
    const body = JSON.stringify(t.schema, null, 2);
    const by = t.granted_by?.rule
      ? `${t.granted_by.layer} · ${esc(t.granted_by.rule)}`
      : (t.granted_by?.layer || "");
    return `<details class="pv-tool">
      <summary>
        <code class="pv-tool-name">${esc(t.name)}</code>
        <span class="pv-flag ${t.read_only ? "ro" : "rw"}">${t.read_only ? "R" : "W"}</span>
        ${t.shell ? `<span class="pv-flag sh">shell</span>` : ""}
        <span class="pv-tool-by">${esc(by)}</span>
        <span class="pv-tool-size">${num(new Blob([body]).size)} B</span>
      </summary>
      <pre class="pv-json">${highlightJson(body)}</pre>
    </details>`;
  }).join("")}</div>`;
}

function deniedHtml(data) {
  const rows = data.denied || [];
  if (!rows.length) return "";
  return `<div class="pv-denied">
    <h5>Not handed to this agent <span class="pv-count">${rows.length}</span></h5>
    <p class="pv-denied-lede">Listed rather than omitted: "why doesn't my agent
      have write" is the question people actually ask, and a missing key answers
      nothing.</p>
    ${rows.map((r) => `<div class="pv-denied-row" data-state="${esc(r.state)}">
      <code>${esc(r.name)}</code>
      <span class="pv-flag ${r.read_only ? "ro" : "rw"}">${r.read_only ? "R" : "W"}</span>
      <span class="pv-denied-why">${esc(r.reason)}</span>
    </div>`).join("")}
  </div>`;
}

function stateHtml(data) {
  if (data.draft) {
    return `<span class="pv-state draft">draft — not saved</span>
      <span class="pv-state-note">resolved live against the current settings,
        with your unsaved edits on top</span>`;
  }
  if (data.frozen) {
    const drifted = data.drift?.changed;
    return `<span class="pv-state frozen">frozen</span>
      <span class="pv-state-note">${drifted
        ? `this session runs the composition it opened with; the files have
           changed since, and a new session would resolve differently`
        : `read from this session's own snapshot — what the model is being sent
           right now`}</span>`;
  }
  return `<span class="pv-state live">live</span>
    <span class="pv-state-note">resolved against the settings files as they are
      now — this is what the next session starts from</span>`;
}

/**
 * Mount the preview pane into `host`. Returns `{ update(data) }`.
 * The tab choice survives every update, because it is a reading position and
 * losing it on every keystroke would make the pane unusable while editing.
 */
export function mountPreview(host) {
  let tab = "prompt";
  let current = null;

  host.className = "wb-preview";
  host.innerHTML = `
    <div class="wb-pv-head">
      <div class="wb-pv-tabs">
        <button class="pv-tab active" data-tab="prompt">System prompt</button>
        <button class="pv-tab" data-tab="tools">Tools</button>
      </div>
      <div class="wb-pv-state"></div>
    </div>
    <div class="wb-pv-body"><div class="set-loading">Resolving…</div></div>
    <div class="wb-pv-foot">
      <span class="pv-foot-text"></span>
      <span class="pv-foot-actions">
        <button class="ghost-btn" data-copy>⧉ Copy</button>
      </span>
    </div>`;

  const body = host.querySelector(".wb-pv-body");
  const foot = host.querySelector(".pv-foot-text");

  function paint() {
    if (!current) return;
    host.querySelector(".wb-pv-state").innerHTML = stateHtml(current);
    host.querySelectorAll(".pv-tab").forEach(
      (b) => b.classList.toggle("active", b.dataset.tab === tab));
    if (tab === "prompt") {
      body.innerHTML = promptHtml(current.prompt || {}) + absencesHtml(current.prompt || {});
      foot.textContent = `${num(current.prompt?.chars)} chars · `
        + `${num(current.prompt?.bytes)} bytes · `
        + `${(current.prompt?.blocks || []).length} blocks · `
        + `${current.prompt?.template || ""}`;
    } else {
      body.innerHTML = toolsHtml(current) + deniedHtml(current);
      foot.textContent = `${current.footer || ""} · `
        + `${num(current.schema_bytes)} bytes of schema`;
    }
    body.scrollTop = 0;
  }

  host.querySelector(".wb-pv-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (!b) return;
    tab = b.dataset.tab;
    paint();
  });

  host.querySelector("[data-copy]").addEventListener("click", async (e) => {
    const text = tab === "prompt"
      ? (current?.prompt?.text || "")
      : JSON.stringify((current?.tools || []).map((t) => t.schema), null, 2);
    try {
      await navigator.clipboard.writeText(text);
      e.currentTarget.textContent = "✓ Copied";
      setTimeout(() => { e.currentTarget.textContent = "⧉ Copy"; }, 1400);
    } catch {
      e.currentTarget.textContent = "clipboard refused";
    }
  });

  return {
    update(data) { current = data; paint(); },
    fail(message) {
      body.innerHTML = `<div class="set-error">${esc(message)}</div>`;
      foot.textContent = "";
    },
    busy() { host.classList.add("is-busy"); },
    idle() { host.classList.remove("is-busy"); },
  };
}
