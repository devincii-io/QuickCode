// Checkpoints and rewind, the part that decides rather than draws: which turns
// changed files, what a rewind preview offers, what the person picked, and
// whether that may be sent. No DOM and no network, so every rule the dialog
// follows runs under node:test. checkpoints/dialog.js draws it; chat/rewind.js
// marks the turns. The API shapes are in docs/CHECKPOINTS.md.

// quickcode/checkpoints/rewind.py MAX_SELECTED: a longer `paths` list is a 400.
export const MAX_SELECTED = 1000;

const REASONS = {
  too_large: "too large to keep a copy of",
  size_cap: "did not fit under this conversation's checkpoint limit",
  evicted: "its saved copy was dropped to make room for newer turns",
  damaged: "its saved copy is missing or damaged",
};

/** Why a file cannot be put back, in words. Blocked reasons arrive as prose. */
export function reasonText(reason) {
  return REASONS[reason] || reason || "not restorable";
}

// What the preview says a rewind would do, and what the result says it did.
const PLANNED = {
  restore: "restore", delete: "delete", create: "re-create", none: "already as before",
};
const DONE = {
  restored: "restored", deleted: "deleted", created: "re-created", unchanged: "unchanged",
};

export function plannedText(action) { return PLANNED[action] || action || ""; }
export function doneText(action) { return DONE[action] || action || ""; }

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** "2 restored, 1 deleted" over `[{action}]`, in a fixed order. */
export function countByAction(files) {
  const counts = new Map();
  for (const f of files || []) counts.set(f.action, (counts.get(f.action) || 0) + 1);
  const order = ["restored", "deleted", "created", "unchanged"];
  const keys = [...order.filter((k) => counts.has(k)),
    ...[...counts.keys()].filter((k) => !order.includes(k))];
  return keys.map((k) => `${counts.get(k)} ${doneText(k)}`).join(", ");
}

// ---- which turns can be rewound --------------------------------------------

/** Turn → the paths it checkpointed and no rewind has undone yet, built from
 *  the log's `checkpoint` and `files_rewound` records as they stream past. */
export class TurnFiles {
  constructor() { this.clear(); }

  clear() { this.turns = new Map(); }

  /** A `checkpoint` record, the main agent's or one wrapped in a subagent's
   *  `agent_event`. Returns the turn it marked, or 0 for anything else. */
  checkpoint(ev) {
    const inner = ev?.type === "agent_event" ? ev.ev : ev;
    if (inner?.type !== "checkpoint" || !inner.path) return 0;
    const turn = Number(inner.turn ?? ev.turn);
    if (!Number.isInteger(turn) || turn < 1) return 0;
    let paths = this.turns.get(turn);
    if (!paths) this.turns.set(turn, paths = new Set());
    paths.add(String(inner.path));
    return turn;
  }

  /** A `files_rewound` record. The entries it undid, at or after the turn it
   *  went back to, take no part in later rewinds. Returns the turns it changed.
   *  A log capped at 200 files cannot name the rest; those turns keep their
   *  mark, and the preview asks the server what is really left. */
  rewound(ev) {
    const to = Number(ev?.to_turn);
    const gone = new Set((ev?.files || []).map((f) => f.path));
    const changed = [];
    if (!Number.isInteger(to) || !gone.size) return changed;
    for (const [turn, paths] of this.turns) {
      if (turn < to) continue;
      let hit = false;
      for (const p of gone) hit = paths.delete(p) || hit;
      if (hit) changed.push(turn);
    }
    return changed;
  }

  count(turn) { return this.turns.get(turn)?.size || 0; }
}

// ---- the dialog's selection -------------------------------------------------

/** What the person has picked out of one preview, and whether it may be sent.
 *
 *  A file that changed since its checkpoint (a conflict) starts unselected:
 *  rewinding it discards that change, which is never the default. "Overwrite
 *  anyway" is the API's `force`, and ticking it takes the conflicted files in. */
export class RewindSelection {
  constructor(preview) {
    this.turn = preview?.turn;
    this.untracked = preview?.untracked || "";
    this.force = false;
    this.rows = (preview?.files || []).map((f) => {
      const conflicts = f.conflicts || [];
      const selectable = f.restorable !== false && !f.blocked;
      return { ...f, conflicts, selectable, selected: selectable && !conflicts.length };
    });
  }

  row(path) { return this.rows.find((r) => r.path === path) || null; }

  get selectable() { return this.rows.filter((r) => r.selectable); }
  get selected() { return this.rows.filter((r) => r.selected); }
  get conflicted() { return this.selectable.filter((r) => r.conflicts.length); }

  /** Selected files that changed since their checkpoint, not cleared to be
   *  overwritten. Any of these and the rewind cannot be sent. */
  get unforced() {
    return this.force ? [] : this.selected.filter((r) => r.conflicts.length);
  }

  toggle(path, on) {
    const r = this.row(path);
    if (!r?.selectable) return;
    r.selected = on === undefined ? !r.selected : !!on;
  }

  /** Every file that may be picked without overwriting anything unasked. */
  setAll(on) {
    for (const r of this.selectable) {
      r.selected = !!on && (!r.conflicts.length || this.force);
    }
  }

  /** "all" | "some" | "none", for the select-all box. */
  allState() {
    const pickable = this.selectable.filter((r) => !r.conflicts.length || this.force);
    const n = pickable.filter((r) => r.selected).length;
    if (!n) return "none";
    return n === pickable.length ? "all" : "some";
  }

  setForce(on) {
    this.force = !!on;
    for (const r of this.conflicted) r.selected = this.force;
  }

  /** `{ok, why}`: whether Rewind may be pressed, and if not, what to do. */
  verdict({ busy = "" } = {}) {
    if (!this.rows.length) return { ok: false, why: "Nothing left to put back." };
    const picked = this.selected;
    if (!picked.length) return { ok: false, why: "Select at least one file." };
    const unforced = this.unforced.length;
    if (unforced) {
      return {
        ok: false,
        why: `${plural(unforced, "selected file")} changed since ${
          unforced === 1 ? "its" : "their"} checkpoint: deselect ${
          unforced === 1 ? "it" : "them"}, or tick “Overwrite anyway”.`,
      };
    }
    if (picked.length > MAX_SELECTED && picked.length < this.rows.length) {
      return { ok: false, why: `Select every file, or at most ${MAX_SELECTED}.` };
    }
    if (busy) return { ok: false, why: busy };
    return { ok: true, why: "" };
  }

  /** The body for POST …/checkpoints/rewind. The paths are named whenever the
   *  API allows, so the rewind does what was previewed and nothing a later
   *  turn added; only "every file, and more than the API takes" omits them. */
  request() {
    const picked = this.selected;
    const body = { turn: this.turn };
    if (!(picked.length === this.rows.length && picked.length > MAX_SELECTED)) {
      body.paths = picked.map((r) => r.path);
    }
    if (this.force && picked.some((r) => r.conflicts.length)) body.force = true;
    return body;
  }

  /** A rewind refused with 409: files changed between the preview and the
   *  click. They are marked, and left out until the person says otherwise.
   *  Returns how many rows it marked. */
  conflictsArrived(conflicts) {
    let marked = 0;
    for (const c of conflicts || []) {
      const r = this.row(c.path);
      if (!r) continue;
      r.conflicts = c.conflicts?.length ? c.conflicts : r.conflicts;
      r.selected = false;
      marked++;
    }
    this.force = false;
    return marked;
  }
}

// ---- what the server said ---------------------------------------------------

/** An API error as `{message, conflicts}`. A 409 from a rewind is either a
 *  conflict (`detail.conflicts`) or a refusal whose words are the server's —
 *  "conversation is busy (a turn is running); …" — and are shown as given. */
export function refusal(err) {
  const detail = err?.detail;
  if (detail && typeof detail === "object") {
    return {
      message: String(detail.message || err.message || "the request failed"),
      conflicts: Array.isArray(detail.conflicts) ? detail.conflicts : [],
      paths: Array.isArray(detail.paths) ? detail.paths : [],
    };
  }
  const text = typeof detail === "string" ? detail : String(err?.message || err || "");
  return { message: text.replace(/^\d{3}:\s*/, "") || "the request failed", conflicts: [], paths: [] };
}

/** Why a rewind would be refused right now, from the pane's own state and in
 *  the server's words (server/checkpoints_api.py `_working`). The server
 *  decides; this only says so before the click rather than after. A
 *  background job is not in the pane's state, so that refusal comes from the
 *  server alone. */
export function busyReason(state) {
  const busy = (why) => `The conversation is busy (${why}); rewind once it is idle.`;
  if (!state) return "";
  if (state.busy) return busy("a turn is running");
  if (state.pending?.length) return busy("a permission prompt is waiting for an answer");
  return "";
}

/** The result of a rewind as a headline plus rows. */
export function resultSummary(result) {
  const files = result?.files || [];
  const skipped = result?.skipped || [];
  const headline = result?.rewind_id
    ? `Rewound to before turn ${result.turn}: ${countByAction(files)}.`
    : "Nothing was put back.";
  return {
    headline,
    files: files.map((f) => ({
      path: f.path,
      action: doneText(f.action),
      // Only a file that was overwritten or removed had anything to keep.
      note: (f.action === "restored" || f.action === "deleted") && !f.backup
        ? "what it replaced was not kept" : "",
    })),
    skipped: skipped.map((s) => ({ path: s.path, reason: reasonText(s.reason) })),
  };
}

/** One line for a `files_rewound` record, in the transcript. */
export function rewoundLine(ev) {
  const total = ev?.file_count ?? (ev?.files || []).length;
  const counts = countByAction(ev?.files || []);
  const parts = [`files rewound to before turn ${ev?.to_turn}`];
  parts.push(counts ? counts : plural(total, "file"));
  if (total > (ev?.files || []).length) parts.push(`${total} in all`);
  if (ev?.skipped?.length) parts.push(`${ev.skipped.length} skipped`);
  if (ev?.forced) parts.push("overwrote changes made since");
  return parts.join(" · ");
}

// ---- the diff ---------------------------------------------------------------

/** Each line of a unified diff with its kind: "meta" | "hunk" | "add" | "del"
 *  | "ctx". The ---/+++ header is only the first two lines, so a removed line
 *  that itself starts with "--" stays a removal. Line endings are kept by the
 *  server (a CRLF → LF rewind is a change); the \r is dropped for display. */
export function diffLines(text) {
  const raw = String(text ?? "").split("\n");
  if (raw.length && raw[raw.length - 1] === "") raw.pop();
  return raw.map((line, i) => {
    const shown = line.endsWith("\r") ? line.slice(0, -1) : line;
    let kind = "ctx";
    if (i < 2 && (line.startsWith("--- ") || line.startsWith("+++ "))) kind = "meta";
    else if (line.startsWith("@@")) kind = "hunk";
    else if (line.startsWith("\\")) kind = "meta";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "del";
    return { kind, text: shown };
  });
}
