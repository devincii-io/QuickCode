// The shared vocabulary of the configuration view: what each kind is called,
// what tile it wears, which hue it owns, and what its card body shows.
//
// The rule the whole view rests on: **kind owns the hue, tier owns the badge
// and the stripe texture**. If kind and tier both owned colour, an amber tool
// card would read as a pending confirmation. The hues themselves are the
// --kind-* tokens in css/config.css, each one an existing theme token or a
// color-mix of one, so swapping the theme swaps them too.
//
// The sigils are two-character ASCII/Latin-1 tiles rather than icons or emoji.
// css/app.css:133 already records that ▸ collapses to a dot in several of the
// mono stacks we fall back to; nothing here can suffer that.

import { esc } from "../util.js";

export const KINDS = {
  tool:           { label: "tool",     sigil: "fn", part: "tools" },
  prompt_section: { label: "prompt",   sigil: "¶",  part: "prompt" },
  provider:       { label: "provider", sigil: "»",  part: "models" },
  agent:          { label: "agent",    sigil: "@",  part: "agents" },
  mcp_server:     { label: "mcp",      sigil: "::", part: "mcp" },
  policy:         { label: "policy",   sigil: "§",  part: "policies" },
  hook:           { label: "hook",     sigil: "()", part: "policies" },
  panel:          { label: "panel",    sigil: "[]", part: "policies" },
  storage:        { label: "storage",  sigil: "db", part: "policies" },
};

/** The Parts pages, in rail order. `kinds` is what each one browses. */
export const PARTS = [
  { slug: "tools",    title: "Tools",              sigil: "fn", kinds: ["tool"] },
  { slug: "prompt",   title: "Prompt",             sigil: "¶",  kinds: ["prompt_section"] },
  { slug: "models",   title: "Models & providers", sigil: "»",  kinds: ["provider"] },
  { slug: "mcp",      title: "MCP servers",        sigil: "::", kinds: ["mcp_server"] },
  { slug: "policies", title: "Policies & limits",  sigil: "§",
    kinds: ["policy", "hook", "storage", "panel"] },
];

export function kindLabel(kind) { return KINDS[kind]?.label || kind; }
export function kindSigil(kind) { return KINDS[kind]?.sigil || "··"; }

/** The tile that opens every card, row and page header. */
export function sigilHtml(kind, { big = false } = {}) {
  return `<span class="k-sigil${big ? " big" : ""}" data-kind="${esc(kind)}"
    title="${esc(kindLabel(kind))}">${esc(kindSigil(kind))}</span>`;
}

export function partForKind(kind) { return KINDS[kind]?.part || "policies"; }

/** Every plugin has exactly one canonical page, whatever else links to it. */
export function canonicalHref(plugin) {
  if (plugin.kind === "agent") {
    return `#/config/agents/${encodeURIComponent(plugin.id)}`;
  }
  return `#/config/parts/${partForKind(plugin.kind)}/${encodeURIComponent(plugin.id)}`;
}

// ---- duplicate-to-customise ----------------------------------------------
//
// `kernel/authoring/store.py` owns the real table and the real sentences; this
// is only the question "is there a button here at all", which the browser has
// to answer before the press. When the answer is no it offers the recourse
// instead of a button that exists in order to fail — and if a refusal is ever
// reached anyway (a kind this table has not heard of), the server's own 400
// carries the full reason and it is rendered verbatim rather than summarised.
//
// Authored anything is duplicable: a copy of a file is a file.

const RECOURSE = {
  tool: ["A built-in tool is Python — its schema, its argument checking and its "
       + "permission shape all come from the class the runtime instantiates, and "
       + "none of that is an argv template.",
         "+ New command tool", "#/config/new/tool"],
  provider: ["A provider is a wire-protocol adapter, which is Python with no "
           + "data shape to copy it into.", "", ""],
  mcp_server: ["MCP servers are configured in settings.json in this version, "
             + "not authored as files.", "", ""],
  policy: ["Nothing consumes a second permission policy.", "", ""],
  hook: ["Nothing consumes a second copy of a loop hook: it would be inert, and "
       + "an inert plugin that looks enabled is worse than no button.", "", ""],
  storage: ["The session log format is fixed by contract.", "", ""],
  panel: ["A panel is frontend code.", "", ""],
};

/** `null` when this plugin can be duplicated, else `{why, label, href}`. */
export function duplicateRefusal(plugin) {
  if (!plugin) return null;
  if (plugin.source === "authored") return null;
  if (plugin.kind === "agent" || plugin.kind === "prompt_section") return null;
  const entry = RECOURSE[plugin.kind];
  if (!entry) return null;
  const [why, label, href] = entry;
  return { why, label, href };
}

// ---- per-kind card bodies -------------------------------------------------
//
// The body is not one template. Each kind shows the fact you would have opened
// the card to find; `facts` carries what only the page around it can know
// (a fetched schema, the prompt's byte ranges, which tools an MCP server
// contributed) and everything degrades to a shorter true line without it.

function setting(plugin, key) {
  return (plugin.settings || []).find((s) => s.key === key);
}

function num(n) { return Number(n || 0).toLocaleString(); }

function flagsHtml(plugin) {
  const ro = plugin.metadata?.read_only;
  return ro
    ? `<span class="k-flag ro" title="Read-only: skips the permission prompt and
        may run in parallel with other reads">R read-only</span>`
    : `<span class="k-flag rw" title="Mutating: goes through the permission
        gate before it runs">W mutating</span>`;
}

function toolBody(plugin, facts) {
  const sig = facts.schemas?.[plugin.id];
  return `<div class="k-body mono">${
    sig ? esc(sig) : `<span class="k-dim">${esc(plugin.title)}(…)</span>`}</div>
    <div class="k-facts">${flagsHtml(plugin)}
      <span class="k-fact">group ${esc(plugin.group || "Tools")}</span>
      ${plugin.source !== "internal"
        ? `<span class="k-fact">from ${esc(plugin.source)}</span>` : ""}</div>`;
}

function promptBody(plugin, facts) {
  const range = facts.ranges?.[plugin.id];
  const body = setting(plugin, "body")?.value || "";
  const first = String(body).split("\n").filter(Boolean).slice(0, 2).join("\n");
  const order = plugin.metadata?.order;
  return `${first ? `<pre class="k-excerpt">${esc(first)}</pre>` : ""}
    <div class="k-facts">
      ${order != null ? `<span class="k-fact">#${esc(order)}</span>` : ""}
      ${range
        ? `<span class="k-fact">${num(range.start)}–${num(range.end)} in the composed
             prompt</span><span class="k-fact">${num(range.end - range.start)} chars</span>`
        : `<span class="k-fact k-dim">not in this session's prompt</span>`}
      ${plugin.metadata?.generated ? `<span class="k-fact">generated</span>` : ""}
    </div>`;
}

function agentBody(plugin) {
  const model = setting(plugin, "model")?.value || "worker";
  const cap = setting(plugin, "mode_cap")?.value || "ask";
  const turns = setting(plugin, "max_turns")?.value ?? 30;
  const tools = plugin.metadata?.tools;
  return `<div class="k-facts">
      <span class="k-fact mono">${esc(model)}</span>
      <span class="k-fact">${tools ? `${tools.length} tools` : "inherits the session's tools"}</span>
      <span class="k-fact">ceiling ${esc(cap)}</span>
      <span class="k-fact">≤${esc(turns)} turns</span>
    </div>`;
}

function mcpBody(plugin, facts) {
  const md = plugin.metadata || {};
  const command = [md.command, ...(md.args || [])].filter(Boolean).join(" ");
  const contributed = facts.mcpTools?.[plugin.id] ?? null;
  return `<div class="k-body mono">${esc(command || "no command recorded")}</div>
    <div class="k-facts">
      <span class="k-fact">${facts.connected?.includes(md.server) ? "connected" : "configured"}</span>
      ${contributed != null
        ? `<a class="k-fact k-link" href="#/config/parts/tools?server=${
             encodeURIComponent(md.server)}">${contributed} tools →</a>`
        : ""}
    </div>`;
}

function providerBody(plugin, facts) {
  const md = plugin.metadata || {};
  return `<div class="k-facts">
    ${md.active ? `<span class="k-fact">active</span>` : `<span class="k-fact k-dim">available</span>`}
    ${facts.endpoint && md.active ? `<span class="k-fact mono">${esc(facts.endpoint)}</span>` : ""}
    ${facts.modelCount != null && md.active
      ? `<span class="k-fact">${num(facts.modelCount)} models</span>` : ""}
  </div>`;
}

function valuesBody(plugin) {
  // policy · hook · storage · panel: the effective values, compactly. This is
  // the fact you would have expanded the card to read.
  const shown = (plugin.settings || []).slice(0, 4).map((s) => {
    const v = Array.isArray(s.value) ? `${s.value.length} entries`
      : String(s.value ?? "").length > 26 ? String(s.value).slice(0, 26) + "…"
      : String(s.value);
    return `<span class="k-fact mono${s.tier === "locked" ? " k-fixed" : ""}"
      title="${esc(s.title || s.key)}${s.tier === "locked" ? " — fixed by design" : ""}"
      >${esc(s.key)}=${esc(v)}</span>`;
  }).join("");
  return `<div class="k-facts">${shown || `<span class="k-fact k-dim">no settings</span>`}</div>`;
}

export function bodyHtml(plugin, facts = {}) {
  switch (plugin.kind) {
    case "tool": return toolBody(plugin, facts);
    case "prompt_section": return promptBody(plugin, facts);
    case "agent": return agentBody(plugin);
    case "mcp_server": return mcpBody(plugin, facts);
    case "provider": return providerBody(plugin, facts);
    default: return valuesBody(plugin);
  }
}

/** `read(file_path, offset?, limit?)` from a tool's declared JSON schema. */
export function signatureOf(schemaText, fallbackName = "") {
  try {
    const s = JSON.parse(schemaText);
    const props = s.parameters?.properties || {};
    const required = new Set(s.parameters?.required || []);
    const args = Object.keys(props).map((k) => (required.has(k) ? k : `${k}?`));
    return `${s.name || fallbackName}(${args.join(", ")})`;
  } catch {
    return "";
  }
}
