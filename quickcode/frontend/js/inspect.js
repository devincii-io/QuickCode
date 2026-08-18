// One inspector, reachable from everywhere.
//
// The Summary / Payload / Result / Timing view lives in the trajectory pane
// and is the only place that renders a raw event. Rather than duplicating it
// per surface, every surface asks for an event by `seq` and the shell decides
// how to show it — which is why this is a registered handler and not a direct
// import: chat and the panels would otherwise have to import the panel shell
// and the trajectory module, and those already import them back.

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
  return `<span class="trace-link" data-seq="${seq}" title="Open in trajectory">${label}</span>`;
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
