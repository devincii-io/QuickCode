// Transient notices: the things that have to be said but have nowhere on the
// page to say them.
//
// The settings pages already have `flash(node, …)`, and where a message has a
// natural home — under the field it is about — that is still the right answer.
// This is for the other case: an action whose form is already gone by the time
// it fails, or which never had one. Until now that case was a `window.alert`,
// which blocks the page, cannot be styled, and reads out as a browser dialog
// rather than as part of the app.
//
// Two rules the timings follow. Something that worked says so briefly and gets
// out of the way; something that failed stays until it is dismissed, because a
// failure the user blinked past is a failure that gets reported as "it just
// did nothing".

import { esc } from "./util.js";

const OK_MS = 4200;
const MAX_STACK = 4;

let host = null;

// The only page-level aria-live host besides trust.js's, and mounted the same
// way: created once, lazily, and never assumed to exist.
function ensureHost() {
  if (host?.isConnected) return host;
  host = document.createElement("div");
  host.id = "toast-host";
  host.className = "toast-host";
  host.setAttribute("aria-live", "polite");
  // The stack is chrome, not content: it must never eat a click meant for the
  // page. Only the toasts themselves take pointer events (see app.css).
  document.body.appendChild(host);
  return host;
}

function dismiss(node) {
  if (!node?.isConnected) return;
  clearTimeout(node._t);
  node.classList.add("toast-out");
  setTimeout(() => node.remove(), 160);
}

/** Show a toast. `kind` is "ok" | "err" | "info"; errors stay until dismissed.
 *  Returns a function that dismisses it early. */
export function toast(text, { kind = "ok", timeout } = {}) {
  const h = ensureHost();
  const node = document.createElement("div");
  node.className = `toast toast-${kind}`;
  if (kind === "err") node.setAttribute("role", "alert");
  node.innerHTML = `<div class="toast-text">${esc(text)}</div>
    <button class="toast-x" aria-label="Dismiss">✕</button>`;
  node.querySelector(".toast-x").addEventListener("click", () => dismiss(node));
  h.appendChild(node);

  // A stack that grows without bound is a wall, not a notification.
  while (h.children.length > MAX_STACK) h.firstElementChild.remove();

  const ms = timeout ?? (kind === "err" ? 0 : OK_MS);
  if (ms > 0) node._t = setTimeout(() => dismiss(node), ms);
  return () => dismiss(node);
}

export function toastOk(text) { return toast(text, { kind: "ok" }); }

// `req()` throws Error("409: detail"); the status code is noise in a toast.
export function toastError(text) {
  return toast(String(text ?? "").replace(/^\d{3}:\s*/, "") || "Something failed",
    { kind: "err" });
}
