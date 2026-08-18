// Page furniture shared by the help sections.
//
// The header, crumb and lede are the configuration view's (.cfg-head,
// .cfg-crumbs, .cfg-head-main, .cfg-lede) rather than a second set that looks
// almost the same. Only .hp-* exists here, and only for the shapes
// Configuration has no equivalent of.

import { esc } from "../util.js";

/** The standard page frame. `body` is trusted markup the caller built. */
export function pageHtml(title, { crumb = "Help", sigil = "?", lede = "", body = "" }) {
  return `<div class="hp-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">${esc(crumb)}</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="panel">${esc(sigil)}</span>
        <h2>${esc(title)}</h2>
      </div>
    </header>
    ${lede ? `<div class="hp-lede">${lede}</div>` : ""}
    ${body}
  </div>`;
}

/** A section heading inside a page. Same rule as .cfg-sub in Configuration. */
export function sub(text, count = null) {
  return `<h3 class="cfg-sub">${esc(text)}${
    count != null ? `<span class="cfg-count">${esc(String(count))}</span>` : ""}</h3>`;
}

/** A sentence the codebase wrote, marked as a quotation.
 *
 *  Every claim on these pages has to be true of *this* code, and the strongest
 *  way to keep a claim true is to quote the module that implements it and name
 *  the file. Then a reader who doubts the page can check it, and a maintainer
 *  who changes the behaviour has the page's citation staring at them in grep. */
export function quote(text, source) {
  return `<blockquote class="hp-quote">${esc(text)}
    <cite>${esc(source)}</cite></blockquote>`;
}

/** The honesty line under a widget.
 *
 *  `live` means the number came from the running backend. `modelled` means this
 *  browser reimplemented a rule that really lives in Python. A widget that does
 *  the second without saying so is how a help page starts quietly lying, so the
 *  line is not optional and every widget carries one. */
export function honesty(kind, text) {
  return `<p class="hp-honesty${kind === "live" ? " live" : ""}">${esc(text)}</p>`;
}

export function note(title, bodyHtml) {
  return `<aside class="hp-note"><h4>${esc(title)}</h4>${bodyHtml}</aside>`;
}

/** A link to a configuration page, styled as body-text link. */
export function link(href, text) {
  return `<a class="hp-a" href="${esc(href)}">${esc(text)}</a>`;
}
