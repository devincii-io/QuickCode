// Help ▸ Keyboard & commands.
//
// The same reference the `?` modal shows, given a URL so the modal has
// somewhere to send you. The modal stays: pressing `?` mid-sentence to remember
// one shortcut should not cost a view transition. This page is where the same
// list can carry the sentence of context each entry deserves.
//
// The two lists are kept in one place — js/help/shortcuts.js — and both this
// page and the modal read it, so the fast reference and the full one cannot
// disagree about what a key does.

import { esc } from "../util.js";
import { KEYS, PANEL_NOTE, SLASH, TERMINAL_NOTE } from "./shortcuts.js";
import { MODES } from "./modes.js";
import { link, note, pageHtml, sub } from "./ui.js";

export async function renderKeyboard(host) {
  const body = `
    ${sub("Keyboard")}
    <dl class="hp-keys">
      ${KEYS.map(([k, d]) => `<dt>${esc(k)}</dt><dd>${esc(d)}</dd>`).join("")}
    </dl>

    ${sub("Slash commands")}
    <p class="hp-p">Type <code>/</code> in the composer to open the menu.
      <kbd>Tab</kbd> completes, <kbd>Enter</kbd> runs, <kbd>Esc</kbd> closes.</p>
    <dl class="hp-keys">
      ${SLASH.map(([cmd, arg, d]) => `
        <dt>${esc(cmd)}${arg ? ` <span class="hp-dim-inline">${esc(arg)}</span>` : ""}</dt>
        <dd>${esc(d)}</dd>`).join("")}
    </dl>

    ${sub("Permission modes, in one line each")}
    <dl class="hp-keys">
      ${MODES.map((m) => `<dt>${esc(m.id)}</dt><dd>${esc(m.what)}</dd>`).join("")}
    </dl>
    <p class="hp-p">${link("#/help/permissions",
      "Permissions & trust")} has the exact behaviour of each one, including the
      two places yolo still stops.</p>

    ${sub("The side panel")}
    <p class="hp-p">${esc(PANEL_NOTE)}</p>

    ${sub("The terminal")}
    <p class="hp-p">${esc(TERMINAL_NOTE)}</p>

    ${note("Escape does one thing at a time", `
      <p class="hp-p"><kbd>Esc</kbd> peels off one layer per press: an open menu
        first, then a dialog, then an expanded panel — and only when none of
        those are showing does it interrupt the agent. That ordering is
        deliberate, so closing a menu can never accidentally stop a running
        turn.</p>`)}
  `;

  host.innerHTML = pageHtml("Keyboard & commands", {
    crumb: "Help",
    sigil: "[]",
    lede: `The same reference the <code>?</code> button shows, with room for the
      context each entry deserves.`,
    body,
  });
}
