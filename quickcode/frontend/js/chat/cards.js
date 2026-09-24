// Tool cards: one call, its arguments, its result and the permission decision
// that gated it. Everything here builds or patches a single card; where a card
// sits in the transcript, and how an event finds it, is chat.js's business.

import { renderAnsiBlock } from "../terminal/emulator.js";
import { argSummary } from "../tool_args.js";
import { highlightToon, toon } from "../toon.js";
import { configTarget } from "../trajectory.js";
import { clickable, el, esc } from "../util.js";

export function traceLink(seq) {
  return `<span class="trace-link" data-seq="${Number(seq)}" title="Open in trajectory">⌕ trace</span>`;
}

function diffBody(name, argsRaw) {
  // For edit: show old/new as a colored diff instead of raw JSON.
  if (name !== "edit") return null;
  let a;
  try { a = JSON.parse(argsRaw || "{}"); } catch { return null; }
  if (!a.old_string && !a.new_string) return null;
  const del = String(a.old_string || "").split("\n")
    .map((l) => `<span class="diff-del">- ${esc(l)}</span>`).join("\n");
  const add = String(a.new_string || "").split("\n")
    .map((l) => `<span class="diff-add">+ ${esc(l)}</span>`).join("\n");
  return `<div class="lbl">${esc(a.file_path || "")}</div><pre>${del}\n${add}</pre>`;
}

// Tools that act on a path: the summary becomes a copyable file reference.
const PATH_TOOLS = new Set(["read", "write", "edit"]);

function summaryHtml(name, argsRaw) {
  const summary = argSummary(name, argsRaw);
  if (!PATH_TOOLS.has(name) || !summary) return esc(summary);
  return `<span class="file-ref" data-path="${esc(summary)}" title="Copy path"
    role="button" tabindex="0">${esc(summary)}</span>`;
}

// The card that governs this tool — the same target the trajectory links to,
// resolved by the same function so the two views cannot drift about which page
// decides what a tool may do.
function configLinkHtml(ev) {
  const target = configTarget(ev);
  if (!target) return "";
  return `<a class="tool-ms k-link" href="${esc(target.href)}"
    title="Open ${esc(target.label)} in configuration — this is what governs it"
    >${esc(target.label)} ↗</a>`;
}

export function toolCardNode(ev, { wireTrace }) {
  const card = el(`<div class="tool-card" data-call="${esc(ev.id)}">
    <div class="tool-head" aria-expanded="false">
      <span class="tool-dot running"></span>
      <span class="tool-name">${esc(ev.name)}</span>
      <span class="tool-summary">${summaryHtml(ev.name, ev.arguments)}</span>
      ${configLinkHtml(ev)}
      <span class="tool-ms tool-took"></span>
    </div>
    <div class="tool-body"></div></div>`);
  const body = card.querySelector(".tool-body");
  const diff = diffBody(ev.name, ev.arguments);
  const input = diff ? `<div class="io-diff">${diff}</div>` : toonBlock(ev.arguments);
  body.innerHTML =
    `<details class="io io-in"><summary><span class="io-tag">IN</span>
       <span class="io-hint">arguments</span></summary>${input}</details>` +
    `<div class="result-slot"></div>` +
    (ev.seq != null ? `<div class="io-trace">${traceLink(ev.seq)}</div>` : "");
  const head = card.querySelector(".tool-head");
  clickable(head, (e) => {
    // The head toggles the card, but it now contains a link. Without this the
    // link both navigates and collapses the card behind it.
    if (e?.target?.closest?.("a")) return;
    head.setAttribute("aria-expanded", String(card.classList.toggle("open")));
  });
  wireFileRefs(card);
  wireTrace(card);
  return card;
}

// A JSON payload as TOON — the encoding the model was actually handed, so the
// card shows what was sent rather than a JSON re-render of it. The model's
// arguments arrive as a string it wrote, which can be truncated or malformed,
// and a tool result is only sometimes JSON: anything that will not parse falls
// back to the raw text, because losing the call is worse than losing the shape.
// Escape bytes, or a carriage return doing an in-place redraw. `bash` cleans
// its own output, so what reaches here is from a tool the boundary does not
// control: an MCP server, a plugin. Drawn as text those are line noise, and a
// `\r` progress bar is a few hundred near-identical lines. The panel's
// emulator already knows what a terminal would have shown, so use it.
const ANSI_RE = /\x1b[[\]()]|\r[^\n]/;   // eslint-disable-line no-control-regex

function toonBlock(raw) {
  const text = String(raw ?? "");
  if (ANSI_RE.test(text)) {
    return `<pre class="code ansi">${renderAnsiBlock(text)}</pre>`;
  }
  const head = text.trim()[0];
  // Only an object or an array is worth re-encoding. Without this a bash
  // result that happens to be the single word `12345` would parse as JSON and
  // come back through TOON with its whitespace quietly rewritten.
  if (head !== "{" && head !== "[") return `<pre class="code">${esc(text)}</pre>`;
  let parsed;
  try { parsed = JSON.parse(text); } catch { return `<pre class="code">${esc(text)}</pre>`; }
  const encoded = toon(parsed);
  // `{}` and `[]` encode to nothing at all. Showing the two characters the
  // model sent says more than an empty box.
  if (!encoded.trim()) return `<pre class="code">${esc(text)}</pre>`;
  return `<pre class="toon">${highlightToon(encoded)}</pre>`;
}

export function resultHtml(content, isError) {
  const text = String(content ?? "");
  const pane = toonBlock(text);
  const tag = isError ? "ERR" : "OUT";
  return `<details class="io io-out${isError ? " io-error" : ""}"${isError ? " open" : ""}>
      <summary><span class="io-tag">${tag}</span>
      <span class="io-hint">${isError ? "error" : "result"}</span></summary>${pane}</details>`;
}

// Clicking a path copies it: there is no editor to open it in, and a link
// that silently does nothing is worse than one that does something small.
function wireFileRefs(node) {
  node.querySelectorAll(".file-ref").forEach((ref) => {
    clickable(ref, (e) => {
      e?.stopPropagation?.();
      const path = ref.dataset.path || "";
      navigator.clipboard?.writeText(path).then(() => {
        ref.classList.add("copied");
        setTimeout(() => ref.classList.remove("copied"), 900);
      }, () => { /* clipboard blocked: leave the text selectable */ });
    });
  });
}

// ---- permissions ----
//
// A decision belongs to the call it gates. It used to render as a chip of its
// own under the card, which repeated the tool and the argument already on the
// card and — being a node that is not a tool call — closed the step the card
// was sitting in, so every gated call split its round in two. The badge and the
// detail block below live inside the card instead.

const LOCKS = {
  pending: { glyph: "🔒", hint: "waiting for your decision" },
  allowed: { glyph: "🔓", hint: "allowed" },
  // The struck lock, not a bare ✗: the glyph has to keep saying "permission"
  // next to a status dot that is already red for a failed call.
  denied: { glyph: "🔒", hint: "denied" },
};

export function markPerm(card, state, reqEv, resEv) {
  card.dataset.perm = state;
  const lock = LOCKS[state];
  let badge = card.querySelector(".tool-lock");
  if (!badge) {
    badge = el(`<span class="tool-lock"></span>`);
    card.querySelector(".tool-head .tool-dot").after(badge);
  }
  badge.textContent = lock.glyph;
  badge.title = `Permission — ${lock.hint}`;
  const body = card.querySelector(".tool-body");
  let block = body.querySelector(".io-perm");
  if (!block) {
    block = el(`<details class="io io-perm"><summary><span class="io-tag">ASK</span>
      <span class="io-hint"></span></summary><div class="perm-detail"></div></details>`);
    body.insertBefore(block, body.querySelector(".result-slot"));
  }
  block.querySelector(".io-hint").textContent = `permission — ${lock.hint}`;
  block.querySelector(".perm-detail").innerHTML = permDetailHtml(reqEv, resEv);
  // Open for the two states that want an answer or explain a failure, closed
  // for a plain "allowed" — the same call `io-error` makes one block down.
  block.open = state !== "allowed";
}

function permDetailHtml(reqEv, resEv) {
  const verdict = !resEv ? "waiting for your decision"
    : resEv.allow
      ? (resEv.persist ? "allowed, and remembered as a rule" : "allowed, this once")
      : "denied";
  const rows = [`<div class="lbl">decision</div><div class="perm-line">${esc(verdict)}</div>`];
  // The preview is the tool's own rendering of the call and the argument is
  // what the rules matched on; showing both put the same string on screen
  // twice for every path tool. The preview wins when there is one.
  const asked = reqEv?.preview || reqEv?.arg || resEv?.arg || "";
  if (asked) rows.push(`<div class="lbl">what it asked to do</div><pre>${esc(asked)}</pre>`);
  if (reqEv?.rule_suggestion) {
    rows.push(`<div class="lbl">rule offered</div>
      <div class="perm-line perm-code">${esc(reqEv.rule_suggestion)}</div>`);
  }
  if (reqEv?.agent && reqEv.agent !== "main") {
    rows.push(`<div class="lbl">agent</div><div class="perm-line">${esc(reqEv.agent)}</div>`);
  }
  return rows.join("");
}
