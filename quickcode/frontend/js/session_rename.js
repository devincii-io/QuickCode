// Renaming one session.

import { toastOk } from "./toast.js";
import { closeModal, modal } from "./ui/modal.js";
import { esc, oneLine } from "./util.js";

/** Rename one session.
 *
 *  Shared by the two places a session is listed — the switcher popover and the
 *  Home view — which differ only in which project the write is addressed to,
 *  hence `save`. The dialog keeps its own failure under the field rather than
 *  raising a toast: the form is still on screen, so the message has a home, and
 *  the toast is for the success, which arrives after the form is gone.
 *
 *  A rename is broadcast on `window` because the session being renamed may be
 *  the one that is open, and the chip and the tab strip in the top bar have to
 *  stop showing the old name. */
export function openRenameSession({ convId, title = "", save, onDone }) {
  const m = modal(
    "Rename session",
    `<div style="font-size:13px;color:var(--fg-dim);margin-bottom:10px">
       A name of your own, instead of the first thing you typed. Clear the field
       to go back to that derived name.</div>
     <input class="deny-input" id="rename-title" spellcheck="false" maxlength="200"
            placeholder="e.g. flaky auth test" value="${esc(title)}">
     <div class="rn-err"></div>`,
    `<button class="btn" data-close>Cancel</button>
     <button class="btn primary" data-save>Save name</button>`,
  );
  const input = m.querySelector("#rename-title");
  const err = m.querySelector(".rn-err");
  const btn = m.querySelector("[data-save]");
  input.focus();
  input.select();

  const commit = async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    err.textContent = "";
    let result;
    try {
      result = await save(input.value);
    } catch (e) {
      btn.disabled = false;
      err.textContent = e.message;
      input.focus();
      return;
    }
    const named = result?.title || input.value.trim();
    closeModal();
    toastOk(`Renamed to “${oneLine(named, 60)}”.`);
    window.dispatchEvent(new CustomEvent("qc:session-renamed", {
      detail: { convId, title: named },
    }));
    if (onDone) onDone(named);
  };

  btn.addEventListener("click", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
  });
  return m;
}
