// Multi-select, and saying honestly what a bulk action over one did.
//
// One selection model, shared by the three lists that have one: the session
// rows on a Home card, the session rows in the switcher popover, and the
// project cards. It is deliberately dumb — a set of ids plus the anchor a
// shift-click extends from — and it never re-renders anything itself. Callers
// mutate it and then redraw, which is what lets a selection survive a re-render
// (the ids outlive the DOM) while a caller that drops the model on view change
// gets the other half of the rule for free.

import { toast, toastError, toastOk } from "./toast.js";

/** A set of selected ids with shift-range extension. `order` is always the ids
 *  currently on screen, in display order — the model holds no DOM. */
export function makeSelection() {
  const ids = new Set();
  let anchor = null;
  return {
    get size() { return ids.size; },
    has: (id) => ids.has(id),
    clear() {
      const had = ids.size > 0;
      ids.clear();
      anchor = null;
      return had;   // so Escape can tell whether it consumed the keystroke
    },
    /** Toggle one row. With `shift`, every row between the anchor and this one
     *  takes this row's new state — the ordinary list-box behaviour. */
    toggle(id, order = [], shift = false) {
      const from = order.indexOf(anchor);
      const to = order.indexOf(id);
      if (shift && anchor !== null && from !== -1 && to !== -1 && from !== to) {
        const on = !ids.has(id);
        const [lo, hi] = from < to ? [from, to] : [to, from];
        for (const other of order.slice(lo, hi + 1)) {
          if (on) ids.add(other); else ids.delete(other);
        }
      } else {
        if (ids.has(id)) ids.delete(id); else ids.add(id);
      }
      anchor = id;
    },
    setAll(order, on) {
      for (const id of order) { if (on) ids.add(id); else ids.delete(id); }
      anchor = null;
    },
    /** Forget ids that are no longer on screen. Called after every reload, so a
     *  row deleted by somebody else cannot linger in a count. */
    keepOnly(order) {
      const live = new Set(order);
      for (const id of [...ids]) if (!live.has(id)) ids.delete(id);
    },
    inOrder(order) { return order.filter((id) => ids.has(id)); },
  };
}

const REASON_WORDS = {
  live: (n) => `${n} still open`,
  missing: (n) => `${n} already gone`,
  unknown: (n) => `${n} no longer listed`,
  failed: (n) => `${n} could not be deleted`,
};

/** Report a bulk result honestly: three of five is never "done".
 *
 *  `skipped` is the backend's per-row list, `[{reason, …}]`; both bulk routes
 *  answer in that shape. A partial result is deliberately not an error toast —
 *  something did happen — but it never reads as a plain success either. */
export function reportBulk(doneCount, skipped, { one, many, verb = "Deleted" }) {
  const count = (n) => `${n} ${n === 1 ? one : many}`;
  const groups = new Map();
  for (const s of skipped || []) {
    groups.set(s.reason, (groups.get(s.reason) || 0) + 1);
  }
  const why = [...groups].map(([reason, n]) =>
    (REASON_WORDS[reason] || ((k) => `${k} skipped`))(n)).join(", ");
  if (!why) {
    if (doneCount) toastOk(`${verb} ${count(doneCount)}.`);
    return;
  }
  if (!doneCount) {
    toastError(`Nothing was deleted — ${why}.`);
    return;
  }
  toast(`${verb} ${count(doneCount)}; ${skipped.length} left alone (${why}).`,
    { kind: "info", timeout: 7000 });
}
