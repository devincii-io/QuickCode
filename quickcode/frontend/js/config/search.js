// The global search box.
//
// Configuration used to open on a flat list of every plugin in the install.
// Reorganising it around agents was right — "make it stop rewriting my whole
// file" is a sentence about an agent, not about a registry — but it cost the
// one thing the flat list was actually good at: *I know the name, take me
// there*. That is a lookup instrument, and a lookup instrument belongs behind
// a search box, not in the navigation where it competes with the pages that
// teach. This is that box.
//
// It searches four things, because four things in this view have a page:
// plugins, the settings on them, agents (by agent id, which is what the
// trajectory and the logs call them) and compositions. Every hit is an address
// under `#/config/…`; nothing here renders a plugin, it only finds one.
//
// Self-contained: the classes are the ones config.css already defines for the
// results dropdown (`.cfg-result`, `.cr-*`), and `.cfg-result.first` doubles as
// the keyboard cursor, so arrow-key navigation costs no new CSS.

import { esc } from "../util.js";
import { canonicalHref, kindLabel, sigilHtml } from "./kinds.js";
import { summaryOf } from "./explain.js";

const MAX_ROWS = 40;

// Rank buckets, lowest first. The ordering is the whole point: someone who
// types "bash" wants `tool.bash`, not the four plugins whose help text
// mentions a shell.
const EXACT = 0, ID_PREFIX = 1, TITLE_PREFIX = 2, ID_PART = 3, TITLE_PART = 4,
      BODY = 5;
const SETTING_PENALTY = 10;   // a knob ranks under the plugin that owns it

function rank(needle, { id = "", title = "" }, haystack) {
  const lid = id.toLowerCase();
  const ltitle = String(title || "").toLowerCase();
  if (lid === needle || ltitle === needle) return EXACT;
  // `tool.bash` should be found by "bash", so the last segment counts as a
  // prefix too — nobody types the kind prefix they are already looking at.
  const tail = lid.slice(lid.indexOf(".") + 1);
  if (lid.startsWith(needle) || tail.startsWith(needle)) return ID_PREFIX;
  if (ltitle.startsWith(needle)) return TITLE_PREFIX;
  if (lid.includes(needle)) return ID_PART;
  if (ltitle.includes(needle)) return TITLE_PART;
  return haystack.includes(needle) ? BODY : -1;
}

/**
 * Every destination in the configuration view that matches `q`.
 *
 * @param {object} ctx   the config view context ({kernel, presets, …})
 * @param {string} q     the raw query
 * @returns {Array<{kind, title, id, sub, href, plugin?, key?}>}
 */
export function searchAll(ctx, q) {
  const needle = String(q || "").trim().toLowerCase();
  if (!needle || !ctx) return [];
  const scored = [];

  for (const p of ctx.kernel?.plugins || []) {
    const hay = `${p.id} ${p.title} ${p.description} ${p.group} ${p.kind}`.toLowerCase();
    const score = rank(needle, p, hay);
    if (score >= 0) {
      scored.push([score, {
        kind: p.kind, title: p.title || p.id, id: p.id, sub: summaryOf(p),
        href: canonicalHref(p), plugin: p,
      }]);
    }
    for (const s of p.settings || []) {
      const shay = `${s.key} ${s.title} ${s.help}`.toLowerCase();
      const sscore = rank(needle, { id: s.key, title: s.title }, shay);
      if (sscore < 0) continue;
      scored.push([sscore + SETTING_PENALTY, {
        kind: p.kind, title: s.title || s.key, id: `${p.id}.${s.key}`,
        sub: `setting on ${p.title} · ${s.tier}`,
        href: canonicalHref(p), plugin: p, key: s.key,
      }]);
    }
  }

  // Agents answer to two names: the plugin id (`agent.explore`) and the agent
  // id (`explore`), which is what the trajectory, the logs and the `agent`
  // tool's schema all use. Searching only the first would make the name people
  // actually see unfindable.
  for (const p of ctx.kernel?.plugins || []) {
    if (p.kind !== "agent") continue;
    const bare = p.id.replace(/^agent\./, "");
    if (bare.toLowerCase() === needle || bare.toLowerCase().startsWith(needle)) {
      scored.push([EXACT, {
        kind: "agent", title: p.title || bare, id: bare,
        sub: "agent — open its workbench", href: canonicalHref(p), plugin: p,
      }]);
    }
  }

  for (const p of ctx.presets?.presets || []) {
    const hay = `${p.id} ${p.title} ${p.description}`.toLowerCase();
    const score = rank(needle, p, hay);
    if (score < 0) continue;
    scored.push([score, {
      kind: "composition", title: p.title || p.id, id: p.id,
      sub: p.id === ctx.presets?.active ? "composition — active" : "composition",
      href: `#/config/compositions/${encodeURIComponent(p.id)}`,
    }]);
  }

  scored.sort((a, b) => a[0] - b[0] || a[1].id.localeCompare(b[1].id));
  // One row per address+setting: an agent found twice is still one page.
  const seen = new Set();
  const out = [];
  for (const [, row] of scored) {
    const key = `${row.href}|${row.key || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(row);
    if (out.length >= MAX_ROWS) break;
  }
  return out;
}

function tile(row) {
  return row.kind === "composition"
    ? `<span class="k-sigil" title="composition">{}</span>`
    : sigilHtml(row.kind);
}

/** Paint `rows` into the dropdown. An empty query closes it; a query with no
 *  hits says so rather than closing, because a silent box reads as broken. */
export function paintResults(box, rows, q) {
  if (!box) return;
  if (!String(q || "").trim()) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = rows.length
    ? rows.map((r, i) => `<a class="cfg-result${i === 0 ? " first" : ""}"
        href="${esc(r.href)}" data-key="${esc(r.key || "")}"
        data-id="${esc(r.plugin?.id || "")}">
        ${tile(r)}
        <span class="cr-title">${esc(r.title)}</span>
        <code class="cr-id">${esc(r.id)}</code>
        <span class="cr-sub">${esc(r.sub || kindLabel(r.kind))}</span>
      </a>`).join("")
    : `<div class="cfg-result empty">Nothing matches “${esc(q)}”.</div>`;
}

/** The hint an empty, focused box shows: what there is to search. */
export function paintHint(box, ctx) {
  if (!box) return;
  const plugins = ctx?.kernel?.plugins?.length || 0;
  const comps = ctx?.presets?.presets?.length || 0;
  if (!plugins) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="cfg-result empty">Search ${plugins} plugins, their
    settings and ${comps} composition${comps === 1 ? "" : "s"} — by id
    (<code>tool.bash</code>), by name, or by what they do.</div>`;
}

function move(box, delta) {
  const rows = [...box.querySelectorAll(".cfg-result[href]")];
  if (!rows.length) return;
  const at = rows.findIndex((r) => r.classList.contains("first"));
  const next = Math.max(0, Math.min(rows.length - 1, (at < 0 ? 0 : at) + delta));
  rows.forEach((r, i) => r.classList.toggle("first", i === next));
  rows[next].scrollIntoView({ block: "nearest" });
}

/**
 * Wire the box once. Returns a `close()` for callers that need to dismiss it.
 *
 * @param {object}   o
 * @param {Element}  o.input     the `<input>`
 * @param {Element}  o.results   the dropdown container
 * @param {Function} o.getCtx    () => the current config context (may be null)
 * @param {Function} [o.onPick]  ({id, key, href}) => void, before navigation
 */
export function initSearch({ input, results, getCtx, onPick }) {
  if (!input || !results) return { close: () => {} };
  const close = () => paintResults(results, [], "");
  const run = () => {
    const q = input.value;
    if (!q.trim()) { paintHint(results, getCtx()); return; }
    paintResults(results, searchAll(getCtx(), q), q);
  };

  input.addEventListener("input", run);
  input.addEventListener("focus", () => { if (!input.value.trim()) paintHint(results, getCtx()); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Stop here: Escape in a search box means "clear this box", not "close
      // whatever modal you think is open behind it".
      e.stopPropagation();
      input.value = "";
      close();
      input.blur();
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      move(results, e.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (e.key !== "Enter") return;
    const active = results.querySelector(".cfg-result.first[href]")
      || results.querySelector(".cfg-result[href]");
    if (active) active.click();
  });

  results.addEventListener("click", (e) => {
    const row = e.target.closest(".cfg-result[href]");
    if (!row) return;
    onPick?.({ id: row.dataset.id || "", key: row.dataset.key || "",
               href: row.getAttribute("href") });
    input.value = "";
    close();
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".cfg-search-wrap")) close();
  });

  return { close, run };
}
