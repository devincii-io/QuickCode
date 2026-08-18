// Help ▸ The big picture — the turn diagram.
//
// This is the page the rest of Help hangs off, so it is the one that has to be
// exactly right. Every box is a real stage in `quickcode/core/loop.py`, in the
// order that file runs them, and every box links to the Settings page for the
// plugin that governs it. Learning the diagram is therefore learning where to
// go when you want to change something.
//
// The colours are not decoration and not new: a box takes the hue the rest of
// the app already gives that thing. Your message is --chip-user because that is
// what the transcript paints a user message; the model's turn is
// --chip-assistant; the tool step is --chip-tool, the same amber as every tool
// card in Settings. Someone who has used the app for ten minutes can read this
// diagram without a key, and the key below is for the first ten minutes.
//
// Built from divs and borders. The one thing a border cannot draw is the
// round-trip — a tool result does not flow onward, it flows *back* — so that
// single edge is an inline SVG, and it is the only SVG on the page.

import { esc } from "../util.js";
import { getFacts } from "./view.js";
import { honesty, link, note, pageHtml, quote, sub } from "./ui.js";

// Every plugin id the diagram links to. Checked against the live kernel before
// the link is offered: an install without one of these (a stripped build, a
// future rename) gets the section index instead of a page that would render
// "there is no plugin X in this project".
const PARTS_INDEX = {
  policy: "#/config/parts/policies",
  prompt: "#/config/parts/prompt",
  tool: "#/config/parts/tools",
  model: "#/config/parts/models",
};

function pluginHref(kernel, id, fallback) {
  const known = kernel?.plugins?.some((p) => p.id === id);
  return known ? `#/config/parts/policies/${encodeURIComponent(id)}` : fallback;
}

// ---- the diagram ----------------------------------------------------------

function node({ step, title, body, href, go = "", kind = "", nodeId = "" }) {
  const attrs = `${kind ? ` data-kind="${esc(kind)}"` : ""}${
    nodeId ? ` data-node="${esc(nodeId)}"` : ""}`;
  return `<a class="hp-node" href="${esc(href)}"${attrs}>
    <div class="hp-node-head">
      <span class="hp-node-step">${esc(step)}</span>
      <span class="hp-node-title">${esc(title)}</span>
      ${go ? `<span class="hp-node-go">${esc(go)} →</span>` : ""}
    </div>
    <div class="hp-node-body">${body}</div>
  </a>`;
}

function edge(label = "") {
  return `<div class="hp-edge" aria-hidden="true">${
    label ? `<span class="hp-edge-label">${label}</span>` : ""}</div>`;
}

/** The round trip: results do not flow onward, they flow back to step 3.
 *  A U-turn is the one connector here that a border cannot express, so it is
 *  drawn — and it is the only drawing on the page. */
function loopBack() {
  return `<div class="hp-loopback">
    <svg viewBox="0 0 240 62" width="240" height="62" role="img"
         aria-label="Tool results return to step 3 and the next round begins">
      <path d="M14 4 L14 30 Q14 46 34 46 L206 46 Q226 46 226 30 L226 12"
            fill="none" stroke="currentColor" stroke-width="1.5"
            stroke-linecap="round"></path>
      <path d="M226 2 L221 13 L231 13 Z" fill="currentColor"></path>
    </svg>
    <p class="hp-loopback-note">The results are pushed back as tool messages —
      one per call, consecutively, in a single batch — and the next round starts
      again at step 3. That repetition is the whole agent loop.</p>
  </div>`;
}

function diagramHtml(kernel) {
  const permHref = pluginHref(kernel, "runtime.permissions", PARTS_INDEX.policy);
  const loopHref = pluginHref(kernel, "runtime.agent_loop", PARTS_INDEX.policy);
  const logHref = pluginHref(kernel, "runtime.session_log", PARTS_INDEX.policy);
  const compactHref = pluginHref(kernel, "runtime.compaction", PARTS_INDEX.policy);
  const subHref = pluginHref(kernel, "runtime.subagents", PARTS_INDEX.policy);
  const protoHref = pluginHref(kernel, "runtime.tool_protocol", PARTS_INDEX.policy);

  return `<div class="hp-diagram-wrap">
    <div class="hp-diagram">
      <div class="hp-spine">

        ${node({
          step: "1", nodeId: "you", href: "#/help/tutorial", go: "Walkthrough",
          title: "You send a message",
          body: `Before it is handed over, any <code>system-reminder</code> queued
            for this turn is appended to it — a note that the conversation was
            just compacted, or that you changed the permission mode since the
            last turn.`,
        })}

        ${edge(`<b>plus the reminders</b>`)}

        ${node({
          step: "2", kind: "prompt_section", href: "#/config/parts/prompt",
          go: "Prompt sections",
          title: "The request is assembled",
          body: `One system prompt, composed from the enabled prompt sections in
            <code>order</code> (ties broken by id, so the bytes are stable and
            cacheable), then the whole conversation so far, then the JSON schema
            of every tool this agent is currently offered.`,
        })}

        ${edge()}

        ${node({
          step: "3", nodeId: "model", href: "#/config/parts/models",
          go: "Models & providers",
          title: "The model answers, streaming",
          body: `Text, reasoning and tool-call fragments arrive as deltas and are
            re-emitted on the event bus as they land. When the response is
            complete it either asked for tools, or it did not.`,
        })}

        <div class="hp-fork" aria-hidden="true">
          <div class="hp-edge"><span class="hp-edge-label">it asked for tools</span></div>
          <div class="hp-edge"><span class="hp-edge-label">it asked for nothing</span></div>
        </div>

        <div class="hp-branch">
          <div class="hp-branch-label">If it asked for tools</div>

          ${node({
            step: "4", kind: "policy", href: permHref, go: "Permissions",
            title: "The permission gate",
            body: `Per call, in this order: the loop hooks get a chance to answer
              it instead; the arguments are validated against the tool's declared
              schema; then the gate decides <b>allow</b>, <b>ask</b> or
              <b>deny</b> from the mode and the rule lists.`,
          })}

          ${edge(`<b>allow</b>, or you approved the <b>ask</b>`)}

          ${node({
            step: "5", kind: "tool", href: "#/config/parts/tools", go: "Tools",
            title: "The tools run",
            body: `The calls whose tool declares itself read-only are gathered and
              run together; everything else runs alone, in the order the model
              asked for. A tool that fails does not break the turn — the error
              comes back as the call's result and the model reads it.`,
          })}

          ${loopBack()}
        </div>

        <div class="hp-branch is-terminal">
          <div class="hp-branch-label">If it asked for nothing</div>
          ${node({
            step: "6", nodeId: "answer", href: "#/help/handson", go: "Try it",
            title: "That is the answer, and the turn ends",
            body: `A response with no tool calls is how a turn finishes normally.
              The status goes back to <code>idle</code> and the app waits for
              you.`,
          })}
        </div>
      </div>

      <aside class="hp-taps">
        <div class="hp-taps-head">Hanging off the loop</div>

        <a class="hp-tap" href="${esc(protoHref)}" data-kind="policy">
          <div class="hp-tap-title">The event bus</div>
          <div class="hp-tap-body">Every stage above emits events. One subscriber
            fans them out: to your browser over the websocket, and — for the
            record types that are kept — to the session log.</div>
        </a>

        <a class="hp-tap" href="${esc(logHref)}" data-kind="storage">
          <div class="hp-tap-title">The session log</div>
          <div class="hp-tap-body">One append-only <code>.jsonl</code> file per
            conversation under <code>.quickcode/sessions/</code>. Every kept event
            gets a sequence number that only goes up, which is what makes resume
            and replay exact rather than approximate.</div>
        </a>

        <a class="hp-tap" href="${esc(logHref)}" data-kind="panel">
          <div class="hp-tap-title">The trajectory</div>
          <div class="hp-tap-body">The side panel's reading of that same log,
            addressed by sequence number. It is not a second recording — it is
            the file, rendered.</div>
        </a>

        <a class="hp-tap" href="${esc(compactHref)}" data-kind="hook">
          <div class="hp-tap-title">Compaction</div>
          <div class="hp-tap-body">Checked <em>between</em> turns, never in the
            middle of one. When the last request filled enough of the context
            window, the older part of the conversation is replaced by a summary
            and the most recent turns are carried through word for word.</div>
        </a>

        <a class="hp-tap" href="${esc(subHref)}" data-kind="hook">
          <div class="hp-tap-title">Subagents</div>
          <div class="hp-tap-body">A tool call like any other from the loop's point
            of view — but the child runs this whole diagram again, with its own
            budget and a permission ceiling it cannot exceed.</div>
        </a>

        <a class="hp-tap" href="${esc(loopHref)}" data-kind="hook">
          <div class="hp-tap-title">The round budget</div>
          <div class="hp-tap-body">Steps 3 to 5 repeat until the model stops asking
            for tools or the budget runs out. At the budget the agent is told to
            wrap up and report state, so a stuck turn ends with a handover rather
            than in silence.</div>
        </a>
      </aside>
    </div>
  </div>

  <div class="hp-diagram-key">
    <span class="hp-key-swatch" data-node="you"><i></i> you</span>
    <span class="hp-key-swatch" data-kind="prompt_section"><i></i> the prompt</span>
    <span class="hp-key-swatch" data-node="model"><i></i> the model</span>
    <span class="hp-key-swatch" data-kind="policy"><i></i> policy</span>
    <span class="hp-key-swatch" data-kind="tool"><i></i> tools</span>
    <span class="hp-key-swatch" data-kind="hook"><i></i> loop lifecycle</span>
    <span class="hp-key-swatch" data-kind="storage"><i></i> storage</span>
    <span>— the same hues the cards in Settings use. Every box is a link.</span>
  </div>`;
}

// ---- the live numbers -----------------------------------------------------

/** What this install actually contains, counted off the kernel rather than
 *  written down here. A number in prose goes stale the day someone adds a
 *  tool; a number read from the registry cannot. */
function countsHtml(kernel) {
  const by = (kind) => kernel.plugins.filter((p) => p.kind === kind).length;
  const rows = [
    ["tools the model can call", by("tool"), "#/config/parts/tools"],
    ["prompt sections", by("prompt_section"), "#/config/parts/prompt"],
    ["agent definitions", by("agent"), "#/config/agents"],
    ["providers", by("provider"), "#/config/parts/models"],
    ["MCP servers", by("mcp_server"), "#/config/parts/mcp"],
    ["policies, hooks, storage and panels", by("policy") + by("hook")
      + by("storage") + by("panel"), "#/config/parts/policies"],
  ];
  const total = kernel.plugins.length;
  return `<p class="hp-p">Everything in the diagram is a plugin, and this install
    is currently running <strong>${esc(String(total))}</strong> of them:</p>
    <ul class="hp-list">
      ${rows.map(([label, n, href]) => `<li class="hp-li">
        <a class="hp-a" href="${esc(href)}"><strong>${esc(String(n))}</strong>
        ${esc(label)}</a></li>`).join("")}
    </ul>
    ${honesty("live", "Counted from the plugin registry this session is running, "
      + "not from a number written into this page.")}`;
}

// ---- the page -------------------------------------------------------------

export async function renderOverview(host) {
  const body = `
    ${sub("One turn, end to end")}
    <div id="hp-diagram-slot">
      <div class="hp-loading">Reading the plugin kernel, so the boxes link to the
        pages this install actually has…</div>
    </div>

    ${sub("What the diagram leaves out on purpose")}
    <p class="hp-p">Three things are true of every box above and would be noise
      repeated six times.</p>
    <ul class="hp-list">
      <li class="hp-li"><strong>Nothing here is privileged.</strong> The tools,
        the prompt sections, the provider, the permission policy, the session
        log — all of them are plugins declared the same way a third-party one
        would be. There is no side door that the shipped parts use and yours
        cannot.</li>
      <li class="hp-li"><strong>The list you see is the list that runs.</strong>
        Settings reads the same registry the loop reads. If those two could
        disagree, every page in Settings would be a guess.</li>
      <li class="hp-li"><strong>A session's composition is frozen when it
        opens.</strong> Changing a setting mid-conversation does not rewrite the
        conversation you are in; it applies to the next one. That is why so many
        pages in Settings say “new sessions”.</li>
    </ul>

    ${quote("Everything QuickCode can do is a plugin: the tools, the prompt "
      + "sections, the providers, the agents, the MCP servers, the loop hooks. "
      + "The ones we ship are “internal” plugins — same shape as a "
      + "third-party one, no privileged side door.",
      "quickcode/kernel/spec.py")}

    ${sub("What this install is made of")}
    <div id="hp-counts-slot"><div class="hp-loading">Counting…</div></div>

    ${note("Where to go next", `
      <p class="hp-p">If Settings looks like a lot, that is because it is showing
        you the whole install on purpose — nothing is hidden from you, including
        the parts you cannot change. ${link("#/help/plugins",
          "The plugin model")} explains why that is a design commitment rather
        than an oversight, and ${link("#/help/questions",
          "the six questions")} teaches you to read any one of those cards in
        about fifteen seconds.</p>`)}
  `;

  host.innerHTML = pageHtml("The big picture", {
    crumb: "Help",
    sigil: "◎",
    lede: `Everything in this app is one loop: your message becomes a request, the
      model answers it, and anything it wants to <em>do</em> goes through a gate
      before it happens. The diagram below is that loop, in the order
      <code>core/loop.py</code> runs it. Every box is a link into the page that
      governs it.`,
    body,
  });

  const facts = await getFacts();
  const slot = host.querySelector("#hp-diagram-slot");
  const counts = host.querySelector("#hp-counts-slot");
  if (!slot) return;   // navigated away while the kernel was loading

  // No kernel: the diagram is still completely true, it just cannot verify that
  // every plugin it wants to link to exists here. It links to the section
  // indexes instead, which always do.
  slot.innerHTML = diagramHtml(facts?.kernel || null);
  if (counts) {
    counts.innerHTML = facts?.kernel
      ? countsHtml(facts.kernel)
      : `<div class="hp-degraded">The plugin kernel could not be read just now, so
          the counts are left out rather than guessed. The diagram above does not
          depend on them.</div>`;
  }
}
