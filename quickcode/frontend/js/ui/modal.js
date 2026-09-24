// The dialog layer: one modal at a time, drawn into #modal-root.
//
// Opening a modal wipes whichever one is on screen. Modules that need to know
// when that happens (the review queue has to put a displaced review back)
// register with onModalClose rather than this file knowing about them, which
// is what keeps the layer free of the socket and loadable in the outer
// workspace window.

import { sheetOpen } from "../settings/ui.js";
import { el, esc } from "../util.js";

export const modalRoot = () => document.getElementById("modal-root");

// Escape has to close the dialog wherever the focus sits — clicking a settings
// tab leaves it on a button, not inside the modal — so the listener is
// document-level, added on open and dropped on close.
let modalEsc = null;
const closeHooks = new Set();

/** Call `fn` every time the modal root is emptied. Returns the unsubscribe. */
export function onModalClose(fn) {
  closeHooks.add(fn);
  return () => closeHooks.delete(fn);
}

export function closeModal() {
  if (modalEsc) {
    document.removeEventListener("keydown", modalEsc, true);
    modalEsc = null;
  }
  modalRoot().innerHTML = "";
  for (const fn of closeHooks) fn();
}

// A review dialog is the agent waiting on an answer, so it does not close on
// Escape, the backdrop, or a ✕ — "no" is the Deny button, which the agent hears.
export function modal(title, bodyHtml, footHtml = "", { dismissible = true } = {}) {
  closeModal();
  // A dropdown or the command palette sits above the backdrop; a dialog that
  // opens under one (a permission review arriving mid-menu) would be covered.
  document.querySelectorAll(".menu").forEach((menu) => (menu.closeMenu ? menu.closeMenu() : menu.remove()));
  const m = el(`<div class="modal-backdrop"><div class="modal" tabindex="-1"
       role="dialog" aria-modal="true" aria-label="${esc(String(title))}">
    <div class="modal-head"><span>${title}</span>
      ${dismissible ? `<button class="ghost-btn" data-close>✕</button>` : ""}</div>
    <div class="modal-body">${bodyHtml}</div>
    ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
  </div></div>`);
  if (dismissible) {
    m.addEventListener("click", (e) => {
      if (e.target === m || e.target.closest("[data-close]")) closeModal();
    });
    modalEsc = (e) => {
      if (e.key !== "Escape") return;
      // A menu or a Settings sheet sits on top: those peel off first, one layer
      // per keystroke, instead of the dialog closing from underneath them.
      if (document.querySelector(".menu") || sheetOpen()) return;
      e.preventDefault();
      // Capture phase plus stopImmediatePropagation: the composer's Escape
      // (interrupt) and the panel's un-maximize must not also fire.
      e.stopImmediatePropagation();
      closeModal();
    };
    document.addEventListener("keydown", modalEsc, true);
  }
  modalRoot().appendChild(m);
  // Move focus into the dialog: Escape and Tab should belong to it from the
  // first keystroke, not to whatever button opened it.
  m.querySelector(".modal").focus();
  return m;
}

/** A yes/no the caller can await. Resolves false on cancel, Escape or backdrop.
 *
 * `window.confirm` would do this in one line and is not used anywhere in the
 * app: it blocks the whole window, which in a WebView2 shell means the socket
 * stops being read and a streaming turn stalls behind a dialog nobody is
 * looking at. Everything that needs an answer goes through the modal layer.
 */
export function confirmModal({ title, body, confirm = "Confirm", danger = true }) {
  return new Promise((resolve) => {
    let answered = false;
    const done = (value) => {
      if (answered) return;
      answered = true;
      resolve(value);
    };
    const m = modal(
      esc(title),
      `<div class="cf-body">${body}</div>`,
      `<button class="btn" data-close>Cancel</button>
       <button class="btn ${danger ? "danger" : "primary"}" data-yes>${esc(confirm)}</button>`,
    );
    m.querySelector("[data-yes]").addEventListener("click", () => {
      done(true);
      closeModal();
    });
    // Every other way out of the dialog is a no, and there are four of them
    // (✕, Cancel, backdrop, Escape). Watching for the node leaving the DOM
    // catches all four without wiring each one.
    new MutationObserver((_records, obs) => {
      if (!m.isConnected) {
        obs.disconnect();
        done(false);
      }
    }).observe(modalRoot(), { childList: true });
  });
}
