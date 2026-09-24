// The event inspector: Summary / Payload / Result / Timing for one logged event.
//
// A component, not a view: `createInspector(root)` builds into any container
// and knows nothing about where it sits. The trajectory mounts it beside its
// table; every other surface reaches it through inspect.js, which routes an
// event's `seq` to wherever the shell decided the inspector lives.
//
// Everything that came from the event log is placed with textContent or text
// nodes. Tool results carry arbitrary text from the shell, the web and MCP
// servers, and the inspector is the one view that shows all of it unabridged.

import { renderJson, prettyJson } from "./json_view.js";
import { toolResultFor } from "./store.js";
import { clock, fmtDur, fmtRel } from "./trajectory/format.js";
import { configTarget, innerOf, roleOf } from "./trajectory/roles.js";
import { node } from "./ui/dom.js";
import { fmtMs } from "./util.js";

const TABS = [
  ["summary", "Summary"], ["payload", "Payload"], ["result", "Result"], ["timing", "Timing"],
];

// Rendered before a "show all" button. Large enough for any system prompt,
// small enough that a multi-megabyte tool result cannot stall the pane.
const SHOW_LIMIT = 120000;

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function fmtSize(chars) {
  if (chars < 1000) return `${chars} chars`;
  if (chars < 1e6) return `${(chars / 1000).toFixed(1)}k chars`;
  return `${(chars / 1e6).toFixed(2)}M chars`;
}

/** A `<pre>` that shows the first SHOW_LIMIT characters and offers the rest.
 *  JSON is highlighted; everything else is plain text. */
function textBlock(text, { json = false, cls = "" } = {}) {
  const wrap = node("div", "insp-block");
  const pre = node("pre", cls || null);
  const full = String(text ?? "");
  const paint = (all) => {
    const shown = all ? full : full.slice(0, SHOW_LIMIT);
    if (json) renderJson(pre, shown); else pre.textContent = shown;
  };
  paint(false);
  wrap.appendChild(pre);
  if (full.length > SHOW_LIMIT) {
    const more = node("button", "ghost-btn insp-more",
      `Show all ${fmtSize(full.length)} (${fmtSize(full.length - SHOW_LIMIT)} more)`);
    more.type = "button";
    more.addEventListener("click", () => { paint(true); more.remove(); });
    wrap.appendChild(more);
  }
  return wrap;
}

function kvGrid(rows) {
  const grid = node("div", "kv");
  for (const [k, v, href] of rows) {
    grid.appendChild(node("div", "k", k));
    const cell = node("div", "v");
    if (href) {
      const a = node("a", "k-link", `${v} ↗`);
      a.href = href;
      a.title = "Open it in configuration";
      cell.appendChild(a);
    } else {
      cell.textContent = String(v);
    }
    grid.appendChild(cell);
  }
  return grid;
}

function section(label) { return node("div", "insp-label", label); }

// What each tab says about an event, given the context the host can supply.
// Separate from the component so a host with a richer model (the trajectory's
// inferred spans, its agent → definition map) can say more than the log does.
function defaultContext() {
  return {
    target: (ev) => configTarget(ev),
    result: (ev) => {
      const inner = innerOf(ev);
      if (inner.type === "tool_result") return inner;
      if (inner.type === "tool_call") return toolResultFor(inner.id, ev.agent_id) ?? null;
      return null;
    },
    timing: () => null,
  };
}

function summaryTab(ev, ctx) {
  const inner = innerOf(ev);
  const target = ctx.target(ev);
  const rows = [
    ["Type", inner.type || ev.type],
    ["Turn", ev.turn ?? "–"],
    ["Sequence", ev.seq],
    ev.agent_id ? ["Agent", ev.agent_id] : null,
    inner.name ? ["Tool", inner.name] : null,
    inner.finish_reason ? ["Finish", inner.finish_reason] : null,
    inner.is_error != null ? ["Status", inner.is_error ? "error" : "ok"] : null,
    // The row that turns a record into something you can act on: the page
    // that decided this was allowed to happen.
    target ? ["Governed by", target.label, target.href] : null,
  ].filter(Boolean);
  const text = inner.text ?? inner.content ?? inner.arguments ?? inner.plan ?? inner.message ?? "";
  const body = String(text);
  if (body) rows.push(["Size", `${fmtSize(body.length)} · ${plural(body.split("\n").length, "line")}`]);
  const out = [kvGrid(rows)];
  if (body) {
    const pretty = inner.type === "tool_call" ? prettyJson(body) : null;
    out.push(textBlock(pretty ?? body, { json: pretty != null }));
  }
  if (inner.reasoning) {
    out.push(section("reasoning"), textBlock(inner.reasoning));
  }
  return out;
}

function payloadTab(ev) {
  return [textBlock(JSON.stringify(ev, null, 2), { json: true, cls: "insp-json" })];
}

function resultTab(ev, ctx) {
  const inner = innerOf(ev);
  const result = ctx.result(ev);
  if (!result) {
    const timing = ctx.timing(ev);
    const why = inner.type === "tool_call"
      ? (timing?.running ? "still running" : "no result was logged")
      : "— not applicable —";
    return [kvGrid([["Result", why]])];
  }
  const content = String(result.content ?? "");
  const pretty = prettyJson(content);
  const rows = [["Status", result.is_error ? "error" : "ok"]];
  if (result.ms != null) rows.push(["Duration", fmtMs(result.ms)]);
  rows.push(["Size", fmtSize(content.length)]);
  return [kvGrid(rows), textBlock(pretty ?? content, {
    json: pretty != null, cls: result.is_error ? "is-error" : "",
  })];
}

function timingTab(ev, ctx) {
  const inner = innerOf(ev);
  const t = ctx.timing(ev);
  const started = t ? t.t0 : Date.parse(ev.ts);
  const rows = [["Started", Number.isFinite(started) ? clock(started, { ms: true }) : "–"]];
  if (t?.running) rows.push(["Ended", "still running"]);
  else if (t && t.t1 > t.t0) rows.push(["Ended", clock(t.t1, { ms: true })]);
  const ms = inner.ms ?? ctx.result(ev)?.ms;
  if (ms != null) rows.push(["Duration", fmtMs(ms)]);
  if (t && t.t1 > t.t0) rows.push(["Span", fmtDur(t.t1 - t.t0) + (t.inferred ? " (inferred)" : "")]);
  if (t && t.tMin != null) rows.push(["Offset", fmtRel(t.t0 - t.tMin)]);
  rows.push(["Source", ev.agent_id ? `subagent ${ev.agent_id}` : "main agent"]);
  return [kvGrid(rows)];
}

const RENDER = { summary: summaryTab, payload: payloadTab, result: resultTab, timing: timingTab };

/**
 * Build an inspector into `root`.
 *
 * @param {HTMLElement} root
 * @param {object} [opts]
 * @param {() => void} [opts.onClose]  shows a close button when given
 * @param {object} [opts.context]      overrides for `target(ev)`, `result(ev)`
 *                                     and `timing(ev)` → {t0, t1, running, tMin, inferred}
 */
export function createInspector(root, { onClose, context = {} } = {}) {
  const ctx = { ...defaultContext(), ...context };
  let ev = null;
  let tab = "summary";

  const head = node("div", "traj-detail-head insp-head");
  const title = node("span", "insp-title", "—");
  head.appendChild(title);
  if (onClose) {
    const close = node("button", "ghost-btn", "✕");
    close.type = "button";
    close.title = "Close the inspector";
    close.setAttribute("aria-label", "Close the inspector");
    close.addEventListener("click", () => onClose());
    head.appendChild(close);
  }
  const tabs = node("nav", "detail-tabs");
  tabs.setAttribute("role", "tablist");
  const buttons = TABS.map(([id, label]) => {
    const b = node("button", id === tab ? "active" : "", label);
    b.type = "button";
    b.dataset.tab = id;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", id === tab ? "true" : "false");
    tabs.appendChild(b);
    return b;
  });
  const body = node("div", "detail-body");
  body.setAttribute("role", "tabpanel");
  root.replaceChildren(head, tabs, body);

  const selectTab = (id) => {
    tab = id;
    for (const b of buttons) {
      const on = b.dataset.tab === id;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    }
    render();
  };
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) selectTab(b.dataset.tab);
  });
  tabs.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const i = TABS.findIndex(([id]) => id === tab);
    const next = TABS[(i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length][0];
    selectTab(next);
    buttons.find((b) => b.dataset.tab === next).focus();
    e.preventDefault();
  });

  function render() {
    if (!ev) { title.textContent = "—"; body.replaceChildren(); return; }
    const role = roleOf(ev);
    const inner = innerOf(ev);
    const chip = node("span", `chip chip-${role}`, role);
    title.replaceChildren(chip, document.createTextNode(` #${ev.seq} · ${inner.type || ev.type}`));
    body.replaceChildren(...RENDER[tab](ev, ctx));
    body.scrollTop = 0;
  }

  return {
    get seq() { return ev ? ev.seq : null; },
    get event() { return ev; },
    get tab() { return tab; },
    show(next) { ev = next || null; render(); },
    refresh: render,
    setTab: selectTab,
  };
}
