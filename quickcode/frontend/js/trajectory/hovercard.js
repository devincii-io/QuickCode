// The card under the crosshair: what the event is, when it started and ended,
// and how long it took in total. Built from text nodes — the preview is
// whatever a tool or a model wrote.

import { clock, fmtDur, fmtRel } from "./format.js";
import { previewOf } from "./roles.js";
import { oneLine } from "../util.js";

function span(cls, text) {
  const n = document.createElement("span");
  if (cls) n.className = cls;
  n.textContent = text;
  return n;
}

function div(cls, ...children) {
  const n = document.createElement("div");
  n.className = cls;
  n.append(...children);
  return n;
}

/** Total milliseconds the card reports: start → end on the axis, except for a
 *  result, which is a point in the log but carries its tool's own timing. */
export function totalMs(it) {
  if (it.inner.type === "tool_result" && it.inner.ms != null) return it.inner.ms;
  return Math.max(0, it.t1 - it.t0);
}

export function renderHoverCard(el, it, { tMin, target }) {
  const kind = it.inner.type || it.ev.type;
  const dur = it.t1 - it.t0;
  const ms = Math.round(totalMs(it));
  const offset = ["at " + fmtRel(it.t0 - tMin)];
  if (it.running) offset.push("still running");
  else if (dur) offset.push("spans " + fmtDur(dur) + (it.inferred ? " (inferred)" : ""));
  // A call's span includes any wait for permission; the tool's own time is
  // the other half of that story.
  const ran = it.result?.ms;
  if (ran != null && dur - ran > 50) offset.push("tool ran " + fmtDur(ran));
  if (target) offset.push("governed by " + target.label);
  el.replaceChildren(
    div("hc-head",
      span(`chip chip-${it.role}`, it.role),
      span("hc-kind", kind),
      span("hc-seq", "#" + it.seq)),
    div("hc-prev", oneLine(previewOf(it.ev), 150)),
    div("hc-time",
      span("", clock(it.t0, { ms: true })),
      span("hc-arrow", "→"),
      span("", it.running ? "now" : clock(it.t1, { ms: true })),
      span("hc-dur", `${ms.toLocaleString()} ms`)),
    div("hc-off", offset.join(" · ")),
  );
}
