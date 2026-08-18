// The prompt-section panel beside the editor.
//
// A prompt section is text plus a position, and the position is the part
// people get wrong: `after:` decides where in the composed prompt the text
// lands, `applies_to:` decides whether subagents see it at all, and `when:`
// decides whether it renders in this kind of session. All three are invisible
// in the body, so this panel says them out loud and links to the page where the
// composed result can be read end to end.
//
// It also states the one lifecycle fact that surprises people: the prompt cache
// breakpoint sits on the system message, so the composed bytes must stay stable
// inside a session. An edit here cannot appear in a conversation that is
// already open, and pretending otherwise would be a correctness bug rather than
// a convenience.

import { esc } from "../../util.js";
import { parseFrontmatter, parseList, patchFrontmatter } from "./scaffold.js";

const WHEN_NOTE = {
  always: "in every session",
  plan: "only while the session is in plan mode",
  orchestration: "only when the session can spawn subagents",
  headless: "only in non-interactive runs",
};

export function promptPanel({ ctx, read, write }) {
  return {
    html() {
      const { meta, close } = parseFrontmatter(read());
      const appliesTo = parseList(meta.applies_to || "[main]");
      const when = (meta.when || "always").trim();
      const anchor = (meta.after || "").trim();
      const known = (ctx.kernel?.plugins || [])
        .filter((p) => p.kind === "prompt_section")
        .map((p) => p.id);
      const anchorOk = !anchor || known.includes(anchor);

      return `<div class="ed-side-inner">
        <section class="wb-sec">
          <h4>Where it lands</h4>
          ${close < 0 ? `<div class="dr-unknown">There is no frontmatter block:
            a plugin file opens with <code>---</code>, a set of keys, and a
            closing <code>---</code>.</div>` : ""}
          <dl class="wb-rows">
            <dt>after</dt><dd>${anchor
              ? `<code>${esc(anchor)}</code>${anchorOk ? "" :
                  ` <span class="dr-warn">no section with that id — the file
                    will not load until it names one that exists</span>`}`
              : `<code>${esc(meta.order || "—")}</code>
                 <span class="wb-note">by order number; the internal sections
                 sit at 10–130</span>`}
              <span class="wb-note">the original still renders — you are adding
                a voice after it, not replacing one</span></dd>
            <dt>reaches</dt><dd>${appliesTo.map(
              (a) => `<code class="wb-pattern">${esc(a)}</code>`).join("")
              || `<span class="wb-note">nothing — an empty applies_to means it
                   is composed into no prompt at all</span>`}
              <span class="wb-note">${appliesTo.includes("subagents")
                ? "subagents get it too, so it is repeated in every spawned prompt"
                : "the orchestrator only; subagents never see it"}</span></dd>
            <dt>renders</dt><dd>${esc(WHEN_NOTE[when] || when)}</dd>
          </dl>
          <label class="ed-field"><span>title</span>
            <input class="tp-input" data-fm="title" value="${esc(meta.title || "")}"></label>
          <label class="ed-field"><span>renders</span>
            <select class="tp-input" data-fm="when">
              ${Object.keys(WHEN_NOTE).map((w) => `<option${
                w === when ? " selected" : ""}>${w}</option>`).join("")}
            </select></label>
        </section>

        <section class="wb-sec">
          <h4>The prompt it joins</h4>
          <p class="wb-note block">The system prompt is not one string: it is
            these sections, composed in order and joined by a blank line. A
            section that renders empty is dropped entirely.</p>
          <a class="btn" href="#/config/parts/prompt">Read the composed prompt →</a>
          <p class="wb-note block">Edits take effect in the next session. The
            prompt cache breakpoint sits on the system message, so the composed
            bytes have to stay stable for as long as a session is open — this is
            the one place where "not until you start a new one" is a correctness
            requirement rather than an implementation detail.</p>
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
