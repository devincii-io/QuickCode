// The rewind dialog: what putting files back to before a turn would do, file
// by file with its diff, then doing it for the files picked.
//
// Built from DOM nodes, never markup strings: paths and diffs are whatever the
// agent wrote, and a text node cannot be anything but text. What the dialog
// may send is decided in checkpoints/model.js; this module only draws it.
//
// Diff lines use the transcript's diff colours (.diff-add / .diff-del). The
// Files panel has its own markup-string renderer; neither is shared yet.

import { api } from "../api.js";
import { store, subscribe } from "../store.js";
import { modal, onModalClose } from "../ui/modal.js";
import {
  RewindSelection, busyReason, diffLines, plannedText, reasonText, refusal, resultSummary,
} from "./model.js";

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false || child === "") continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
let serial = 0;   // ids for aria-controls, unique across openings

/** Open the dialog for "before turn `turn`" of the pane's conversation.
 *  `opener` gets the focus back when the dialog closes. */
export function openRewindDialog(turn, { opener = null, convId = store.convId } = {}) {
  if (!convId) return null;
  const m = modal(`Rewind files to before turn ${Number(turn)}`, "", "");
  const box = m.querySelector(".modal");
  box.classList.add("rw-modal");
  const body = m.querySelector(".modal-body");
  const foot = h("div", { class: "modal-foot" });
  box.append(foot);

  const view = { m, body, foot, turn, convId, sel: null, sending: false, done: false, stale: false };
  body.append(h("div", { class: "rw-loading", role: "status" }, "Reading what would change…"));

  // While the dialog is up the conversation goes on: a turn finishing changes
  // whether a rewind is allowed, and one that edits files changes what it does.
  // A rewind of its own is not news: it is what `sending` is waiting for.
  const unsubscribe = subscribe((kind, ev) => {
    if (!m.isConnected || view.done || view.sending) return;
    if (kind === "event" && (ev.type === "checkpoint" || ev.type === "files_rewound"
        || (ev.type === "agent_event" && ev.ev?.type === "checkpoint"))) view.stale = true;
    if (kind === "state" || kind === "status") {
      if (view.stale && !busyReason(store.state)) load(view);
      else refresh(view);
    }
  });
  const unhook = onModalClose(() => {
    unhook();
    unsubscribe();
    // Back to the button that opened it, unless another dialog took its place.
    setTimeout(() => {
      if (opener?.isConnected && !document.querySelector(".modal-backdrop")) opener.focus();
    });
  });
  load(view);
  return m;
}

// Only the newest of overlapping loads draws: a reload asked for while one is
// still in flight must not be painted over by the older answer.
async function load(view) {
  view.stale = false;
  const gen = view.gen = (view.gen || 0) + 1;
  let preview;
  try {
    preview = await api.previewRewind(view.convId, { turn: view.turn });
  } catch (err) {
    if (!view.m.isConnected || view.done || gen !== view.gen) return;
    view.body.replaceChildren(h("div", { class: "rw-error", role: "alert" },
      `Could not preview the rewind: ${refusal(err).message}`));
    view.foot.replaceChildren(closeButton("Close"));
    return;
  }
  if (!view.m.isConnected || view.done || view.sending || gen !== view.gen) return;
  const before = view.sel;
  view.sel = new RewindSelection(preview);
  if (before) keepChoices(before, view.sel);
  render(view);
}

// A reloaded preview keeps what the person had already decided about the files
// it still lists — except a file that has since changed, which starts over.
function keepChoices(old, next) {
  next.force = old.force && next.conflicted.length > 0;
  for (const row of next.rows) {
    const was = old.row(row.path);
    if (!was || !row.selectable) continue;
    if (row.conflicts.length && !was.conflicts.length) continue;
    row.selected = was.selected && (!row.conflicts.length || next.force);
  }
}

// ---- the preview -------------------------------------------------------------

function render(view) {
  const { sel } = view;
  view.rows = new Map();
  const parts = [
    h("p", { class: "rw-intro" },
      `Puts the files changed in turn ${view.turn} and after back the way they were before it. `
      + "The conversation itself is not rewound: the agent keeps its memory of the edits "
      + "and is told which files came back."),
    view.busyNode = h("div", { class: "rw-busy", role: "status" }),
  ];
  view.msg = h("div", { class: "rw-msg", role: "alert" });
  if (!sel.rows.length) {
    parts.push(h("div", { class: "rw-empty" },
      "Nothing left to put back: every file changed from this turn on has already been rewound."),
    untrackedNote(sel.untracked), view.msg);
    view.body.replaceChildren(...parts);
    view.go = null;
    view.foot.replaceChildren(closeButton("Close"));
    return;
  }
  parts.push(allRow(view), h("div", { class: "rw-files" }, sel.rows.map((r) => fileRow(view, r))));
  if (sel.conflicted.length) parts.push(forceRow(view));
  parts.push(untrackedNote(sel.untracked), view.msg);
  view.body.replaceChildren(...parts);

  view.go = h("button", { class: "btn primary", type: "button", "data-rewind": true,
    onclick: () => send(view) });
  view.foot.replaceChildren(closeButton("Cancel"), view.go);
  refresh(view);
}

function allRow(view) {
  const box = h("input", { type: "checkbox", "aria-label": "Select every file that can be rewound",
    onchange: () => { view.sel.setAll(box.checked); changed(view); } });
  view.allBox = box;
  view.allLabel = h("span");
  return h("label", { class: "rw-all" }, box, view.allLabel);
}

function fileRow(view, row) {
  const id = `rw-diff-${++serial}`;
  const check = h("input", {
    type: "checkbox", "aria-label": `Include ${row.path}`, disabled: !row.selectable,
    onchange: () => { view.sel.toggle(row.path, check.checked); changed(view); },
  });
  const diff = h("div", { class: "rw-diff", id, hidden: true });
  const toggle = h("button", {
    class: "rw-toggle", type: "button", "aria-expanded": "false", "aria-controls": id,
    title: "Show what the rewind would change in this file",
    onclick: () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      diff.hidden = !open;
      if (open && !diff.firstChild) diff.append(...diffBody(row));
    },
  }, h("span", { class: "rw-caret", "aria-hidden": "true" }), h("span", { class: "rw-path" }, row.path));
  const node = h("div", { class: "rw-file", "data-path": row.path },
    h("div", { class: "rw-row" }, check, toggle,
      h("span", { class: `rw-action rw-${row.action || "none"}` }, plannedText(row.action)),
      counts(row)),
    conflictList(row),
    row.selectable ? null
      : h("div", { class: "rw-reason" }, `Left alone: ${reasonText(row.blocked || row.reason)}.`),
    diff);
  view.rows.set(row.path, { node, check });
  return node;
}

function counts(row) {
  if (!row.selectable) return h("span", { class: "rw-counts" });
  if (row.omitted === "binary" || row.binary) return h("span", { class: "rw-counts" }, "binary");
  if (row.omitted === "too_large") return h("span", { class: "rw-counts" }, "too large to compare");
  if (row.added == null && row.removed == null) return h("span", { class: "rw-counts" });
  return h("span", { class: "rw-counts", title: `${row.added || 0} lines added, ${
    row.removed || 0} removed by the rewind` },
  h("span", { class: "diff-add" }, `+${row.added || 0}`), " ",
  h("span", { class: "diff-del" }, `−${row.removed || 0}`));
}

function conflictList(row) {
  if (!row.conflicts.length) return null;
  // The server's sentence already says what changed and when.
  const sentence = (text) => String(text || "").replace(/^./, (c) => c.toUpperCase());
  return h("ul", { class: "rw-conflicts" }, row.conflicts.map((c) =>
    h("li", { "data-kind": c.kind }, h("span", { "aria-hidden": "true" }, "⚠ "), sentence(c.detail))));
}

function diffBody(row) {
  if (!row.selectable) return [h("div", { class: "rw-hint" }, "Not rewound, so there is nothing to compare.")];
  if (row.omitted === "binary") return [h("div", { class: "rw-hint" }, "A binary file: no text diff.")];
  if (row.omitted === "too_large") {
    return [h("div", { class: "rw-hint" }, "Too large to compare (over 2 MB on one side).")];
  }
  if (!row.diff) {
    return [h("div", { class: "rw-hint" },
      row.action === "none" ? "No change: the file already holds what the rewind would write."
        : "No text diff.")];
  }
  const pre = h("pre", { class: "rw-pre" });
  const lines = diffLines(row.diff);
  lines.forEach((line, i) => {
    const cls = { add: "diff-add", del: "diff-del", hunk: "diff-hunk", meta: "diff-meta" }[line.kind];
    pre.append(cls ? h("span", { class: cls }, line.text) : line.text);
    if (i < lines.length - 1) pre.append("\n");
  });
  const out = [h("div", { class: "rw-legend" }, "− on disk now   + after the rewind"), pre];
  if (row.truncated) out.push(h("div", { class: "rw-hint" }, "Diff cut at 100 000 characters."));
  return out;
}

function forceRow(view) {
  const n = view.sel.conflicted.length;
  const box = h("input", { type: "checkbox",
    onchange: () => { view.sel.setForce(box.checked); changed(view); } });
  view.forceBox = box;
  return h("label", { class: "rw-force" }, box,
    h("span", {}, h("strong", {}, "Overwrite anyway. "),
      `${plural(n, "file")} changed since ${n === 1 ? "its" : "their"} checkpoint; `
      + "rewinding discards that change as well."));
}

function untrackedNote(text) {
  return h("div", { class: "rw-untracked" },
    h("strong", {}, "Bash changes are not tracked. "),
    text || "Files changed by bash, by MCP tools or outside QuickCode are not put back.");
}

function closeButton(label) {
  return h("button", { class: "btn", type: "button", "data-close": true }, label);
}

// A choice made after an error message is an answer to it.
function changed(view) {
  view.msgSticky = false;
  refresh(view);
}

// Everything that depends on the selection or the conversation's state, in one
// place, so no control can disagree with another about what Rewind would do.
function refresh(view) {
  const { sel } = view;
  if (!sel || view.done || !view.go) return;
  const busy = busyReason(store.state);
  view.busyNode.textContent = busy;
  view.busyNode.hidden = !busy;
  for (const row of sel.rows) {
    const drawn = view.rows.get(row.path);
    if (!drawn) continue;
    drawn.check.checked = row.selected;
    drawn.node.classList.toggle("rw-conflicted", row.conflicts.length > 0);
    drawn.node.classList.toggle("rw-off", !row.selected);
  }
  if (view.allBox) {
    const state = sel.allState();
    view.allBox.checked = state === "all";
    view.allBox.indeterminate = state === "some";
    view.allLabel.textContent = `${plural(sel.selected.length, "file")} of ${sel.rows.length} selected`;
  }
  if (view.forceBox) view.forceBox.checked = sel.force;
  const verdict = sel.verdict({ busy });
  const n = sel.selected.length;
  view.go.textContent = view.sending ? "Rewinding…" : `Rewind ${plural(n, "file")}`;
  view.go.disabled = view.sending || !verdict.ok;
  view.go.classList.toggle("danger", sel.force && n > 0);
  view.go.classList.toggle("primary", !(sel.force && n > 0));
  if (!view.msgSticky) {
    view.msg.textContent = verdict.ok || verdict.why === busy ? "" : verdict.why;
  }
}

// ---- the rewind ------------------------------------------------------------------

async function send(view) {
  const { sel } = view;
  if (view.sending || !sel.verdict({ busy: busyReason(store.state) }).ok) return;
  view.sending = true;
  view.msgSticky = false;
  refresh(view);
  let result;
  try {
    result = await api.rewindFiles(view.convId, sel.request());
  } catch (err) {
    view.sending = false;
    if (!view.m.isConnected) return;
    const why = refusal(err);
    if (err?.status === 409 && why.conflicts.length) {
      const n = sel.conflictsArrived(why.conflicts);
      render(view);
      view.msg.textContent = `${plural(n, "file")} changed since the preview and ${
        n === 1 ? "was" : "were"} left out. Nothing was rewound.`;
    } else {
      refresh(view);
      view.msg.textContent = `Not rewound: ${why.message}`;
    }
    view.msgSticky = true;
    return;
  }
  view.sending = false;
  view.done = true;
  if (view.m.isConnected) showResult(view, result);
}

function showResult(view, result) {
  const s = resultSummary(result);
  const parts = [h("p", { class: "rw-headline", role: "status" }, s.headline)];
  if (s.files.length) {
    parts.push(h("ul", { class: "rw-done" }, s.files.map((f) =>
      h("li", {}, h("span", { class: "rw-path" }, f.path), " ",
        h("span", { class: "rw-action" }, f.action),
        f.note ? h("span", { class: "rw-reason" }, ` — ${f.note}`) : null))));
  }
  if (s.skipped.length) {
    parts.push(h("div", { class: "rw-sub" }, "Left alone"),
      h("ul", { class: "rw-done rw-skipped" }, s.skipped.map((f) =>
        h("li", {}, h("span", { class: "rw-path" }, f.path),
          h("span", { class: "rw-reason" }, ` — ${f.reason}`)))));
  }
  if (result?.rewind_id) {
    parts.push(h("p", { class: "rw-intro" },
      "The agent is told at the start of its next turn which files were put back."));
  }
  parts.push(untrackedNote(result?.untracked));
  view.body.replaceChildren(...parts);
  const close = closeButton("Close");
  close.classList.add("primary");
  view.foot.replaceChildren(close);
  close.focus();
}
