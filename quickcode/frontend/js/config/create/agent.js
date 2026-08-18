// The agent panel beside the editor.
//
// An agent file's body *is* its system prompt, verbatim — which is why the
// textarea is the main surface and this side is a reading of the frontmatter
// rather than a form that owns it.
//
// The one thing this panel must not do is claim to be the truth. What an agent
// finally gets is a *resolution*: the file's tool patterns are matched against
// the live pool, its ceiling is intersected with the spawner's, its model is
// checked against the profile. That answer exists already, on the agent
// workbench, computed by the same resolver the runner uses. So every row here
// is labelled "as written" and the panel's most useful control is the link to
// the page that shows what it becomes.

import { esc } from "../../util.js";
import { parseFrontmatter, parseList, patchFrontmatter } from "./scaffold.js";

const CEILING_NOTE = {
  plan: "may not write anything at all",
  ask: "stops for permission on anything mutating — but a subagent has nobody "
     + "to ask, so this is where it stops",
  "auto-edit": "may edit files without asking; other mutations still stop",
  yolo: "may do anything the tool pool allows without asking",
};

export function agentPanel({ read, write }) {
  return {
    html() {
      const { meta, close } = parseFrontmatter(read());
      const name = meta.name || "";
      const tools = "tools" in meta ? parseList(meta.tools) : null;
      const models = parseList(meta.models || "");
      const cap = (meta.mode_cap || "ask").trim();

      return `<div class="ed-side-inner">
        <section class="wb-sec">
          <h4>As written</h4>
          ${close < 0 ? `<div class="dr-unknown">There is no frontmatter block:
            a plugin file opens with <code>---</code>, a set of keys, and a
            closing <code>---</code>. Everything after it is the body.</div>` : ""}
          <dl class="wb-rows">
            <dt>name</dt><dd><code>${esc(name || "—")}</code></dd>
            <dt>tools</dt><dd>${tools === null
              ? `<span class="wb-note">key absent — inherits whatever the
                  spawning agent holds, narrowed by nothing</span>`
              : tools.length
                ? tools.map((t) => `<code class="wb-pattern">${esc(t)}</code>`).join("")
                : `<span class="wb-note">an empty list: no tools at all</span>`}</dd>
            <dt>model</dt><dd><code>${esc(meta.model || "—")}</code>
              ${models.length
                ? `<span class="wb-note">allowed: ${esc(models.join(", "))}</span>`
                : ""}</dd>
            <dt>ceiling</dt><dd><code>${esc(cap)}</code>
              <span class="wb-note">${esc(CEILING_NOTE[cap]
                || "the most this agent may ever do")}. It is intersected with
                the spawner's, so raising it here cannot lift the agent above
                the session it runs in.</span></dd>
            <dt>max turns</dt><dd>${esc(meta.max_turns || "—")}</dd>
          </dl>
          <label class="ed-field"><span>title</span>
            <input class="tp-input" data-fm="title" value="${esc(meta.title || "")}"></label>
          <label class="ed-field"><span>ceiling</span>
            <select class="tp-input" data-fm="mode_cap">
              ${["plan", "ask", "auto-edit", "yolo"].map((c) => `<option${
                c === cap ? " selected" : ""}>${c}</option>`).join("")}
            </select></label>
          <p class="wb-note block">These rewrite one frontmatter line each and
            leave the rest of the bytes alone — the file stays the document.</p>
        </section>

        <section class="wb-sec">
          <h4>What it becomes</h4>
          <p class="wb-note block">Everything above is what the file says. What
            this agent actually gets is resolved against the live tool pool and
            intersected with whoever spawns it — the workbench shows that answer,
            with provenance on every value and the composed prompt beside it.</p>
          ${name ? `<a class="btn" href="#/config/agents/${encodeURIComponent(name)}"
            >Open the workbench →</a>` : `<span class="wb-note">Give it a
            <code>name:</code> and it gets a page.</span>`}
          <p class="wb-note block">The body below the frontmatter lands at
            <code>&lt;role&gt;</code> in the subagent prompt, between the identity
            block and the environment block. Say what it reads, what it writes,
            and what its final message must contain — that message is the whole
            of what the spawner sees.</p>
        </section>
      </div>`;
    },
    mount(node) {
      for (const el of node.querySelectorAll("[data-fm]")) {
        el.addEventListener("change", () => {
          write(patchFrontmatter(read(), el.dataset.fm, el.value));
        });
      }
    },
  };
}
