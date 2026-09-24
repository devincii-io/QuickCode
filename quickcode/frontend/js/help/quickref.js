// The `?` quick reference.
//
// `?` stays a modal, and that is a decision rather than an oversight. It is
// pressed mid-sentence to remember one shortcut, and answering that with a view
// transition — losing the transcript you were looking at, changing the URL —
// would be a worse app for the commonest question. So the fast reference stays
// exactly where people expect it.
//
// What the modal is no longer allowed to be is the *whole* of the help. It now
// ends in a link into #/help, which is the surface that can explain how the
// pieces fit together, and it reads its two lists from js/help/shortcuts.js so
// the quick reference and the full one cannot drift apart.

import { MODES } from "../modes.js";
import { closeModal, modal } from "../ui/modal.js";
import { esc } from "../util.js";
import { KEYS, PANEL_NOTE, slashRows } from "./shortcuts.js";

export function openHelp({ onFull } = {}) {
  const row = ([k, d]) =>
    `<div class="help-row"><span class="help-key">${k}</span>
       <span class="help-desc">${d}</span></div>`;
  const m = modal("Help", `
    <div class="help-sec">
      <h4>Keyboard</h4>
      ${KEYS.map(([k, d]) => row([esc(k), esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Slash commands</h4>
      ${slashRows().map(([cmd, arg, d]) =>
        row([esc(cmd) + (arg ? " " + esc(arg) : ""), esc(d)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Permission modes</h4>
      ${MODES.map(({ id, summary }) => row([esc(id), esc(summary)])).join("")}
    </div>
    <div class="help-sec">
      <h4>Side panel</h4>
      <div class="help-note">${esc(PANEL_NOTE)}</div>
    </div>`,
    `<button class="btn primary" id="help-full">Open the full help →</button>`);
  // Three places open this modal (the top bar, Home, and /help) and none of
  // them cares where "full help" goes, so the default is the route itself. The
  // callback exists for the one caller that wants to remember where it came
  // from; without it the link still works, which is what keeps this button from
  // ever being the dead control it would otherwise become.
  m.querySelector("#help-full").addEventListener("click", () => {
    closeModal();
    if (onFull) onFull();
    else location.hash = "#/help/overview";
  });
  return m;
}
