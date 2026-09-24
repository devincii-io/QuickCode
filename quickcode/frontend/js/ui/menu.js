// Dropdown menus anchored to a pill or a top-bar chip. One menu at a time:
// opening one removes whatever `.menu` is already on the page.

import { el } from "../util.js";

const GAP = 8;

// `center` places the menu near the top of the window with no anchor (the
// command palette). `onClose` runs when the menu is closed — by Escape, a click
// outside or `closeMenu()` — but not when another menu replaces it.
export function menuAt(anchor, contentHtml, { searchable = false, below = false, center = false, onClose = null, className = "" } = {}) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const m = el(`<div class="menu${className ? ` ${className}` : ""}">
    ${searchable ? '<input class="menu-search" placeholder="Search…">' : ""}
    <div class="menu-list">${contentHtml}</div></div>`);
  document.body.appendChild(m);

  // Pinned to the trigger by the edge that faces it, never by a height measured
  // once: filtering a 400-model list shrinks the menu, and a `top` computed for
  // the tall version would leave it floating far above its pill.
  const place = () => {
    if (center) {
      m.style.top = Math.round(window.innerHeight * 0.12) + "px";
      m.style.bottom = "auto";
      m.style.maxHeight = Math.round(window.innerHeight * 0.72) + "px";
      m.style.left = Math.max(GAP, (window.innerWidth - m.offsetWidth) / 2) + "px";
      return;
    }
    const r = anchor.getBoundingClientRect();
    const room = below ? window.innerHeight - r.bottom - GAP * 2 : r.top - GAP * 2;
    m.style.maxHeight = Math.max(160, Math.min(window.innerHeight * 0.6, room)) + "px";
    // Composer pills open upward; a top-bar anchor has no room above it.
    if (below) {
      m.style.top = r.bottom + GAP + "px";
      m.style.bottom = "auto";
    } else {
      m.style.bottom = window.innerHeight - r.top + GAP + "px";
      m.style.top = "auto";
    }
    m.style.left = Math.max(GAP, Math.min(r.left, window.innerWidth - m.offsetWidth - 12)) + "px";
  };
  place();
  // Freeze the width the full list asked for: without it every keystroke in
  // the search box resizes the card between the min and max width. (A centred
  // menu has a width of its own in CSS.)
  if (!center) m.style.width = m.offsetWidth + "px";

  // The handlers survive `m.remove()` calls made by the callers, so each one
  // unregisters itself the moment the menu is gone.
  const cleanup = () => {
    document.removeEventListener("mousedown", dismiss, true);
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", onResize);
  };
  const gone = () => {
    if (m.isConnected) return false;
    cleanup();
    return true;
  };
  const close = () => {
    const was = m.isConnected;
    m.remove();
    cleanup();
    if (was) onClose?.();
  };
  const dismiss = (e) => { if (!gone() && !m.contains(e.target)) close(); };
  const onKey = (e) => {
    if (gone() || e.key !== "Escape") return;
    e.preventDefault();
    e.stopImmediatePropagation();   // closing a menu must not interrupt the agent
    close();
  };
  // Scrolling the menu's own list keeps the menu; anything else moved the
  // anchor, so follow it rather than leaving a detached card behind.
  const onScroll = (e) => {
    if (gone()) return;
    if (e.target === m || (e.target.nodeType === 1 && m.contains(e.target))) return;
    place();
  };
  const onResize = () => { if (!gone()) place(); };

  setTimeout(() => {
    if (gone()) return;
    document.addEventListener("mousedown", dismiss, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
  }, 0);
  // Callers close through this, so the listeners go with the node. (A stray
  // `m.remove()` elsewhere is still safe: every handler checks isConnected.)
  m.closeMenu = close;
  return m;
}
