// #view-help — Help as a real fourth view, peer of Configuration.
//
// Why a view and not a bigger modal: the same three reasons Configuration is
// one (js/config/view.js:1-14). Every page here deserves a URL, because the
// whole point of this surface is that a card in Settings can say "why is this
// fixed" and link to the paragraph that answers it — and a link into a dialog
// is not a link. The big-picture diagram needs the width. And showing it must
// not disconnect the workspace socket: main.js's showHelp() never calls
// disconnect(), exactly as showConfig() does not.
//
// The keyboard reference stays a modal as well (js/modals.js openHelp). That is
// deliberate and not a duplicate: `?` is pressed mid-sentence to remember one
// shortcut, and making that cost a view transition would be a regression. The
// modal keeps the fast reference and links here; this view holds the same list
// under #/help/keyboard so the modal has a real destination.
//
// Everything structural is borrowed from js/config/view.js rather than
// reinvented, because a second navigation idiom in one app is a bug.

import { esc } from "../util.js";
import { renderRail } from "./rail.js";
import { renderOverview } from "./overview.js";
import { renderPlugins } from "./plugins.js";
import { renderQuestions } from "./questions.js";
import { renderPermissions } from "./permissions.js";
import { renderTutorial } from "./tutorial.js";
import { renderHandsOn } from "./handson.js";
import { renderKeyboard } from "./keyboard.js";

const $ = (id) => document.getElementById(id);

export const DEFAULT_ROUTE = "#/help/overview";

// The pages, in rail order. One table: the rail reads it, the router reads it,
// so a section can never exist in the navigation and not in the router.
export const SECTIONS = [
  { slug: "overview", title: "The big picture", sigil: "◎",
    blurb: "How one message becomes an answer.", render: renderOverview },
  { slug: "plugins", title: "The plugin model", sigil: "::",
    blurb: "What everything in Settings is.", render: renderPlugins },
  { slug: "questions", title: "The six questions", sigil: "¶",
    blurb: "How to read any card in Settings.", render: renderQuestions },
  { slug: "permissions", title: "Permissions & trust", sigil: "§",
    blurb: "What it may do, and what it may run.", render: renderPermissions },
  { slug: "tutorial", title: "Your first session", sigil: "1.",
    blurb: "A walkthrough, in order.", render: renderTutorial },
  { slug: "handson", title: "Hands-on", sigil: "fn",
    blurb: "Three things you can try here.", render: renderHandsOn },
  { slug: "keyboard", title: "Keyboard & commands", sigil: "[]",
    blurb: "Shortcuts and slash commands.", render: renderKeyboard },
];

let currentApi = null;
let wired = false;
let lastRoute = DEFAULT_ROUTE;

export function isHelpRoute(hash) {
  return String(hash || "").startsWith("#/help");
}

/** `#/help/handson?w=rules` → {path:[…], query:{…}}. Same parser shape as the
 *  configuration view's, minus the parts it has no pages for. */
export function parseRoute(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const path = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  if (path[0] === "help") path.shift();
  const query = {};
  for (const [k, v] of new URLSearchParams(queryPart || "")) query[k] = v;
  return { path: path.length ? path : ["overview"], query };
}

export function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

// ---- data -----------------------------------------------------------------
//
// Nothing on these pages *requires* the network: every section is written so
// that it says something true with no data at all, and the live numbers are an
// upgrade rather than a precondition. That is not politeness — a help page that
// breaks when the backend hiccups is the one page you needed at that moment.

let facts = null;      // resolved snapshot, or null
let loading = null;    // in-flight load, shared by concurrent navigations

async function load(api) {
  // `kernel` is the one call worth waiting for: it carries the real plugin
  // inventory the diagram, the kinds page and every widget describe. The other
  // two are decoration and are allowed to fail alone.
  const kernel = await api.kernel();
  const [presets, agents] = await Promise.all([
    api.presets().catch(() => null),
    api.agents().catch(() => null),
  ]);
  return { api, kernel, presets, agents, go };
}

/** The shared snapshot, or null when it could not be read. Sections await this
 *  and branch on null; none of them throws over it. */
export async function getFacts() {
  if (facts) return facts;
  if (!currentApi) return null;
  try {
    loading = loading || load(currentApi);
    facts = await loading;
  } catch {
    facts = null;
  } finally {
    loading = null;
  }
  return facts;
}

export function invalidate() { facts = null; loading = null; }

// ---- rendering ------------------------------------------------------------

export async function render() {
  const page = $("help-page");
  if (!page) return;
  const route = parseRoute(location.hash);
  lastRoute = isHelpRoute(location.hash) ? location.hash : lastRoute;

  const section = SECTIONS.find((s) => s.slug === route.path[0]) || SECTIONS[0];
  renderRail($("help-rail"), route);
  page.scrollTop = 0;

  // Every section paints its own static frame synchronously and fills the live
  // parts in afterwards, so there is no loading state over prose that never
  // needed the network.
  try {
    await section.render(page, route);
  } catch (err) {
    page.innerHTML = `<div class="hp-inner"><div class="set-error">This help page
      failed to render: ${esc(err.message)}<br>Nothing here is essential to
      running the agent — the rest of the app is unaffected.</div></div>`;
    return;
  }
  focusHeading(page);
}

/** Move focus to the page heading on navigation. A rail link that repaints the
 *  main region without moving focus leaves a screen reader reading the rail it
 *  just left, and leaves the keyboard on a link whose page is gone. */
function focusHeading(page) {
  const h = page.querySelector("h1, h2");
  if (!h) return;
  h.setAttribute("tabindex", "-1");
  h.focus({ preventScroll: true });
}

// ---- wiring ---------------------------------------------------------------

export function initHelp({ api, onDone }) {
  currentApi = api;
  if (wired) return;
  wired = true;
  $("help-done")?.addEventListener("click", onDone);
}

export function lastHelpRoute() { return lastRoute; }
