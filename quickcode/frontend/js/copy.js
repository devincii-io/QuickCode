// Copying things out of the app: buttons on anything worth copying, and a
// right-click menu that exists because the one the browser would have given us
// does not appear in a WebView2 window.
//
// INTEGRATION
//   import { initCopy } from "./copy.js";
//   initCopy();              // once, at boot, after the shell is in the DOM
//
// Two decisions worth stating:
//
//   - Decoration is done by observing the DOM rather than by editing every
//     renderer. The transcript, the agent panel, the trajectory inspector and
//     the config views all produce `<pre>` blocks and message bubbles from
//     different code, and a copy button that only works in some of them is
//     worse than none: the user learns it is unreliable and stops looking.
//   - The button copies the *source text*, not the rendered text. A code block
//     that has been syntax-highlighted is a tree of spans; `textContent` puts
//     it back together correctly, which `innerHTML` would not.

import { toastError, toastOk } from "./toast.js";

/** The text of a node, without the copy button this file put inside it.
 *
 * The button has to live inside its block to be positioned against it, and
 * `textContent` walks the whole subtree, so reading the node naively appended
 * the literal word "copy" to the end of every code block anyone copied. Clone
 * and strip rather than read-then-trim: trimming a known suffix would also
 * eat a block whose last line really is `copy`.
 */
function sourceText(node) {
  const clone = node.cloneNode(true);
  clone.querySelectorAll(".copy-btn").forEach((b) => b.remove());
  return clone.textContent;
}

// Where a copy button is worth having, and what it should copy.
const TARGETS = [
  { sel: "pre", label: "Copy code", text: sourceText },
  { sel: ".msg .bubble", label: "Copy message", text: sourceText },
  { sel: ".pa-text", label: "Copy", text: sourceText },
  { sel: ".agent-text", label: "Copy", text: sourceText },
];

let observer = null;

export async function copyText(text, what = "Copied") {
  const value = String(text ?? "");
  if (!value.trim()) return false;
  try {
    // http://127.0.0.1 is a secure context, so this is available in the app
    // window as well as in a browser tab.
    await navigator.clipboard.writeText(value);
    toastOk(`${what} — ${value.length.toLocaleString()} characters`);
    return true;
  } catch {
    // Clipboard permission refused, or an older engine. A hidden textarea and
    // execCommand still work there, and failing silently would be the worst of
    // the three outcomes.
    try {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (!ok) throw new Error("copy refused");
      toastOk(`${what} — ${value.length.toLocaleString()} characters`);
      return true;
    } catch {
      toastError("Could not copy to the clipboard.");
      return false;
    }
  }
}

// ---- buttons ----

function decorate(node) {
  for (const t of TARGETS) {
    if (!node.matches?.(t.sel) || node.dataset.copyReady) continue;
    node.dataset.copyReady = "1";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.textContent = "copy";
    btn.title = t.label;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();     // a bubble inside a collapsible card
      e.preventDefault();
      copyText(t.text(node), t.label);
    });
    // The button is absolutely positioned, so the block it sits in has to be a
    // containing block. Set here rather than in CSS: these elements come from
    // four different stylesheets and one of them would have been missed.
    if (getComputedStyle(node).position === "static") node.style.position = "relative";
    node.appendChild(btn);
    return;
  }
}

function sweep(root) {
  if (!(root instanceof Element)) return;
  decorate(root);
  for (const t of TARGETS) {
    root.querySelectorAll?.(t.sel).forEach(decorate);
  }
}

// ---- right-click menu ----
//
// The app window has no browser chrome, so without this there is no copy at
// all for a user who does not know Ctrl+C — and no paste into the composer for
// one whose habit is the right button.

function menuItems(target, selection) {
  const items = [];
  const pre = target.closest("pre");
  const bubble = target.closest(".msg .bubble, .pa-text, .agent-text");
  const input = target.closest("textarea, input");

  if (selection) {
    items.push({ label: "Copy selection", run: () => copyText(selection, "Copied selection") });
  }
  if (pre) items.push({ label: "Copy code block", run: () => copyText(sourceText(pre), "Copied code") });
  if (bubble) items.push({ label: "Copy message", run: () => copyText(sourceText(bubble), "Copied message") });
  if (input) {
    if (selection) {
      items.push({
        label: "Cut",
        run: async () => {
          if (await copyText(selection, "Cut")) document.execCommand("delete");
        },
      });
    }
    items.push({
      label: "Paste",
      run: async () => {
        try {
          const text = await navigator.clipboard.readText();
          if (!text) return;
          input.focus();
          // setRangeText keeps undo history and the caret, which replacing
          // `value` outright would throw away.
          const start = input.selectionStart ?? input.value.length;
          const end = input.selectionEnd ?? start;
          input.setRangeText(text, start, end, "end");
          input.dispatchEvent(new Event("input", { bubbles: true }));
        } catch {
          toastError("Clipboard read was refused — Ctrl+V still works.");
        }
      },
    });
    items.push({ label: "Select all", run: () => input.select() });
  }
  const transcript = target.closest("#transcript, .pa-body, .panel-pane");
  if (!input && transcript) {
    items.push({
      label: "Copy everything here",
      run: () => copyText(sourceText(transcript), "Copied"),
    });
  }
  return items;
}

function openMenu(x, y, items) {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  menu.setAttribute("role", "menu");
  for (const item of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ctx-item";
    b.textContent = item.label;
    b.addEventListener("click", () => { closeMenu(); item.run(); });
    menu.appendChild(b);
  }
  document.body.appendChild(menu);
  // Placed after measuring, so a menu opened near an edge stays on screen.
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(x, window.innerWidth - r.width - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - r.height - 8)}px`;
  setTimeout(() => {
    document.addEventListener("pointerdown", closeMenu, { once: true });
    document.addEventListener("keydown", onEsc, true);
  }, 0);
}

function onEsc(e) {
  if (e.key !== "Escape") return;
  e.stopImmediatePropagation();   // not an interrupt, not a modal close
  closeMenu();
}

function closeMenu() {
  document.querySelector(".ctx-menu")?.remove();
  document.removeEventListener("keydown", onEsc, true);
}

export function initCopy() {
  sweep(document.body);
  observer?.disconnect();
  observer = new MutationObserver((records) => {
    for (const rec of records) {
      for (const node of rec.addedNodes) sweep(node);
    }
  });
  observer.observe(document.body, { childList: true, subtree: true });

  document.addEventListener("contextmenu", (e) => {
    const target = e.target;
    if (!(target instanceof Element)) return;
    const selection = String(window.getSelection?.() ?? "").trim();
    const items = menuItems(target, selection);
    if (!items.length) return;   // nothing useful to offer: leave the default
    e.preventDefault();
    openMenu(e.clientX, e.clientY, items);
  });
}
