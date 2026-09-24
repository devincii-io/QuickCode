// One inspector, reachable from everywhere.
//
// The Summary / Payload / Result / Timing view is a component (inspector.js)
// that the trajectory pane mounts beside its table. Every other surface asks
// for an event by `seq` and the shell decides how to show it — which is why
// this is a registered handler and not a direct import: chat and the panels
// would otherwise have to import the panel shell and the trajectory module,
// and those already import them back.

import { clickable } from "./util.js";

let handler = null;

/** Called once at boot by main.js with the real implementation. */
export function setInspector(fn) { handler = fn; }

/** Open the inspector on a logged event. No-op before boot wires it up. */
export function inspect(seq) {
  if (handler && Number.isFinite(seq)) handler(Number(seq));
}

/** Markup for an inspect affordance; wire it with `wireInspect(root)`. */
export function inspectLink(seq, label = "⌕ trace") {
  if (seq == null) return "";
  return `<span class="trace-link" data-seq="${Number(seq)}" title="Open in trajectory">${label}</span>`;
}

/** Delegate clicks on any `.trace-link` inside `root` to the inspector. */
export function wireInspect(root) {
  root.querySelectorAll(".trace-link").forEach((link) => {
    if (link.dataset.wired) return;
    link.dataset.wired = "1";
    link.addEventListener("click", (e) => {
      e.stopPropagation();
      inspect(Number(link.dataset.seq));
    });
  });
}

function size(chars) {
  return chars < 1000 ? `${chars} chars` : `${(chars / 1000).toFixed(1)}k chars`;
}

/** The system prompt's place in a transcript: one line saying the model was
 *  given its instructions here, and how much of them, that opens the inspector
 *  on the full text. The prompt itself is far too long to be a message. */
export function promptNote(ev) {
  const node = document.createElement("div");
  node.className = "sys-note sys-prompt";
  node.title = "Open the full system prompt in the inspector";
  const label = document.createElement("span");
  label.textContent = `system prompt · ${size(String(ev.text ?? "").length)}`;
  const link = document.createElement("span");
  link.className = "trace-link";
  link.textContent = "⌕ inspect";
  node.append(label, " ", link);
  if (ev.seq != null) clickable(node, () => inspect(Number(ev.seq)));
  return node;
}
