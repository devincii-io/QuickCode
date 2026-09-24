// Clock and offset formatting for the time axis, the hover card and the
// inspector's Timing tab; durations are util.js fmtMs. Clock times follow the
// reader's locale (12 or 24 hour); the formatters are built once because the
// axis relabels every frame while zooming and `toLocaleTimeString` builds a
// new one per call.

const fmt = (opts) => new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", ...opts });
const HM = fmt({});
const HMS = fmt({ second: "2-digit" });
const HMS1 = fmt({ second: "2-digit", fractionalSecondDigits: 1 });
const HMS3 = fmt({ second: "2-digit", fractionalSecondDigits: 3 });
const DAY = new Intl.DateTimeFormat([], { month: "short", day: "numeric" });

const valid = (t) => Number.isFinite(t) && !isNaN(new Date(t));

/** Wall-clock time of day. `ms` adds milliseconds: a hover card over a 40 ms
 *  tool call that reads "10:04:12 → 10:04:12" has said nothing. */
export function clock(t, { ms = false } = {}) {
  if (!valid(t)) return "–";
  return (ms ? HMS3 : HMS).format(t);
}

/** Day identity, or its label. One function because the caller needs both for
 *  the same instant and they must never disagree. */
export function dayOf(t, label = false) {
  const d = new Date(t);
  return label ? DAY.format(d) : `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** A tick label says only as much of the clock as the tick spacing can
 *  distinguish: minutes across an hour, tenths across a burst of tool calls. */
export function clockLabel(t, step) {
  if (!valid(t)) return "–";
  if (step >= 60000) return HM.format(t);
  if (step >= 1000) return HMS.format(t);
  return HMS1.format(t);
}

/** Offset from the start of the session. */
export function fmtRel(ms) {
  const v = Math.max(0, ms);
  if (v < 10000) return "+" + (v / 1000).toFixed(1) + "s";
  const tot = Math.round(v / 1000);
  const h = Math.floor(tot / 3600), m = Math.floor((tot % 3600) / 60), s = tot % 60;
  const p = (x) => String(x).padStart(2, "0");
  return h ? `+${h}:${p(m)}:${p(s)}` : `+${m}:${p(s)}`;
}
