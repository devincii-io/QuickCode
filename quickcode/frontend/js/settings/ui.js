// Shared Settings primitives: badges, the stacked sheet, the confirm dialog,
// the raw-view inspector and the JSON highlighter.
//
// Settings itself lives in a modal, and several of its affordances (confirm a
// risky change, read a plugin's raw definition) have to appear *over* it
// without destroying it — modals.js `modal()` clears the whole modal root, so
// these open as their own layer inside it instead.

import { el, esc } from "../util.js";

// ---- tier / kind / source badges -----------------------------------------

const TIER_TEXT = {
  free: "Edit freely.",
  confirm: "Changing this changes how the agent behaves — it asks first.",
  locked: "Part of how QuickCode works. Always viewable, never editable.",
};

export function tierBadge(tier, { label = tier } = {}) {
  return `<span class="tier tier-${esc(tier)}" title="${esc(TIER_TEXT[tier] || "")}">${
    tier === "locked" ? "⛊ " : ""}${esc(label)}</span>`;
}

export function chip(text, cls = "") {
  return `<span class="set-chip ${esc(cls)}">${esc(text)}</span>`;
}

export function tierNote(tier) {
  return TIER_TEXT[tier] || "";
}

// ---- stacked sheet --------------------------------------------------------

const modalRoot = () => document.getElementById("modal-root");
const stack = [];

/** A dialog layered over the Settings modal. Returns the node; call
 *  `node.closeSheet()` (or Escape / backdrop click) to dismiss it. */
export function sheet(title, bodyHtml, footHtml = "", { wide = false } = {}) {
  const node = el(`<div class="set-sheet">
    <div class="set-sheet-card${wide ? " wide" : ""}" role="dialog" aria-modal="true"
         aria-label="${esc(String(title).replace(/<[^>]*>/g, ""))}" tabindex="-1">
      <div class="set-sheet-head"><span class="ss-title">${title}</span>
        <button class="ghost-btn" data-sheet-close>✕</button></div>
      <div class="set-sheet-body">${bodyHtml}</div>
      ${footHtml ? `<div class="set-sheet-foot">${footHtml}</div>` : ""}
    </div></div>`);
  // Inside the modal root, so closing Settings can never leave a sheet behind.
  modalRoot().appendChild(node);

  const close = () => {
    const i = stack.indexOf(entry);
    if (i >= 0) stack.splice(i, 1);
    document.removeEventListener("keydown", onKey, true);
    node.remove();
    entry.onClose?.();
  };
  const onKey = (e) => {
    if (e.key !== "Escape") return;
    if (stack[stack.length - 1] !== entry) return;   // only the topmost sheet
    e.preventDefault();
    e.stopImmediatePropagation();                    // Settings itself stays open
    close();
  };
  const entry = { node, close, onClose: null };
  stack.push(entry);
  document.addEventListener("keydown", onKey, true);

  node.addEventListener("click", (e) => {
    if (e.target === node || e.target.closest("[data-sheet-close]")) close();
  });
  node.closeSheet = close;
  node.onSheetClose = (fn) => { entry.onClose = fn; };
  node.querySelector(".set-sheet-card").focus();
  return node;
}

/** True when a sheet is open — modals.js consults this so Escape peels the
 *  layers off one at a time instead of closing Settings from underneath. */
export function sheetOpen() { return stack.length > 0; }

// ---- confirm dialog -------------------------------------------------------

/** The `confirm` tier. `reason` is the server's own words about what breaks —
 *  never a bare "are you sure?". Resolves true when the user goes ahead. */
export function confirmRisk({ title, what, reason, applyLabel = "Change it anyway" }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => { if (!done) { done = true; resolve(ok); } };
    const s = sheet(
      `<span class="tier tier-confirm">confirm</span> ${esc(title || "Confirm change")}`,
      `<div class="cf-what">${what || ""}</div>
       <div class="cf-reason"><span class="cf-reason-mark">!</span><div>${
         esc(reason || "This changes how the agent behaves.")}</div></div>
       <div class="cf-tail">Nothing has been saved yet. Going ahead applies the
         change now and it takes effect for new turns.</div>`,
      `<button class="btn" data-cancel>Keep it as it is</button>
       <button class="btn primary" data-apply>${esc(applyLabel)}</button>`,
    );
    s.onSheetClose(() => finish(false));
    s.querySelector("[data-cancel]").addEventListener("click", () => { finish(false); s.closeSheet(); });
    s.querySelector("[data-apply]").addEventListener("click", () => { finish(true); s.closeSheet(); });
    s.querySelector("[data-apply]").focus();
  });
}

// ---- the raw view ---------------------------------------------------------

const JSON_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** Minimal JSON tokenizer → spans. Deliberately dependency-free: the payloads
 *  here are tool schemas and MCP definitions, not arbitrary source. */
export function highlightJson(src) {
  const out = [];
  let last = 0;
  let m;
  JSON_RE.lastIndex = 0;
  while ((m = JSON_RE.exec(src)) !== null) {
    out.push(esc(src.slice(last, m.index)));
    if (m[1] !== undefined) {
      out.push(`<span class="${m[2] ? "j-key" : "j-str"}">${esc(m[1])}</span>`);
      if (m[2]) out.push(esc(m[2]));
    } else if (m[3] !== undefined) {
      out.push(`<span class="j-lit">${esc(m[3])}</span>`);
    } else {
      out.push(`<span class="j-num">${esc(m[4])}</span>`);
    }
    last = JSON_RE.lastIndex;
  }
  out.push(esc(src.slice(last)));
  return out.join("");
}

export function viewBodyHtml(view) {
  if (!view) {
    return `<div class="set-empty">This plugin does not publish a definition of
      its own — everything it is, is in its settings above.</div>`;
  }
  const body = view.format === "json" || view.format === "schema"
    ? `<pre class="raw json">${highlightJson(view.content || "")}</pre>`
    : `<pre class="raw">${esc(view.content || "")}</pre>`;
  const path = view.path
    ? `<div class="raw-path" title="${esc(view.path)}">on disk: <code>${esc(view.path)}</code></div>`
    : "";
  return `<div class="raw-meta">
      <span class="set-chip">${esc(view.format || "text")}</span>
      <span class="raw-title">${esc(view.title || "")}</span>
      <button class="ghost-btn raw-copy" data-copy>⧉ Copy</button>
    </div>${path}${body}`;
}

/** "Show me what this actually is." Opens for any plugin at any tier — that a
 *  setting is locked is precisely why being able to read it matters. */
export async function openPluginView(api, plugin) {
  const s = sheet(
    `${esc(plugin.title || plugin.id)} <span class="ss-id">${esc(plugin.id)}</span>
     ${tierBadge(plugin.tier)}`,
    `<div class="set-loading">Reading the definition…</div>`,
    `<button class="btn" data-sheet-close>Close</button>`,
    { wide: true },
  );
  const body = s.querySelector(".set-sheet-body");
  let detail;
  try {
    detail = await api.plugin(plugin.id);
  } catch (err) {
    body.innerHTML = `<div class="set-error">Could not read this plugin: ${esc(err.message)}</div>`;
    return s;
  }
  body.innerHTML = `<div class="raw-head">${esc(detail.description || "")}</div>`
    + viewBodyHtml(detail.view);
  body.addEventListener("click", (e) => {
    if (!e.target.closest("[data-copy]")) return;
    navigator.clipboard?.writeText(detail.view?.content || "");
    e.target.textContent = "✓ Copied";
    setTimeout(() => { e.target.textContent = "⧉ Copy"; }, 1200);
  });
  return s;
}

// ---- misc -----------------------------------------------------------------

/** `req()` throws `Error("409: detail")`; recover the two halves. */
export function splitError(err) {
  const m = /^(\d{3}): ([\s\S]*)$/.exec(err?.message || "");
  return m ? { status: Number(m[1]), detail: m[2] } : { status: 0, detail: err?.message || "failed" };
}

export function flash(node, text, kind = "ok") {
  if (!node) return;
  node.className = `set-flash ${kind}`;
  node.textContent = text;
  clearTimeout(node._t);
  if (kind === "ok") node._t = setTimeout(() => { node.textContent = ""; }, 2600);
}
