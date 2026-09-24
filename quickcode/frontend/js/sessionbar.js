// The session bar: the chip, and the tab strip beside it.
//
// The chip shows the open session's own title — derived from the first user
// message unless it has been renamed — and opens the full list. The strip is
// the recently-used conversations as tabs, so switching is one click and you
// can see what else is there.
//
// The strip is emphatically a list of *shortcuts*. js/ws.js allows exactly one
// live socket and connect() resets the event store, so an inactive tab is an
// id and nothing more: clicking it leaves the conversation you are in. The
// titles say so, and no tab ever shows a running indicator.

import { api } from "./api.js";
import { openSessionMenu } from "./sessions_menu.js";
import { store, subscribe } from "./store.js";
import { debounce, esc, oneLine } from "./util.js";

const $ = (id) => document.getElementById(id);

const TAB_LIMIT = 6;          // beyond this the overflow button opens the list

let embedded = false;
let onTitle = () => {};

// A session with nothing in it yet is listed as "(empty)", which is the right
// word for a row in a list of many and the wrong one for the session you are
// sitting in. Here it is the one you just started.
function label(s, n = 90) {
  const title = s?.title && s.title !== "(empty)" ? s.title : "New session";
  return oneLine(title, n);
}

function tabHint(s, active) {
  if (active) return `This session: ${label(s)}`;
  if (embedded) return `Open ${label(s)} in an agent pane`;
  return `Switch to “${label(s)}” — QuickCode runs one conversation at a time, `
    + "so this leaves the one you are in.";
}

function renderSessionTabs(sessions, convId, mine) {
  const strip = $("session-tabs");
  if (!strip) return;
  // The archive is deliberately absent: a filed-away session is not a recent
  // one, and it is one click away in the popover, which does list it.
  const listed = sessions.filter((s) => !s.archived);
  const shown = listed.slice(0, TAB_LIMIT);
  // Whatever else has been touched more recently, the open session is a tab:
  // a switcher that cannot show you where you are is not a switcher. A brand
  // new conversation may not be in the list yet, so it gets a placeholder.
  if (convId && !shown.some((s) => s.conv_id === convId)) {
    if (shown.length >= TAB_LIMIT) shown.pop();
    shown.push(mine || { conv_id: convId, title: "New session" });
  }
  const hidden = Math.max(0, listed.length - shown.length);
  const tab = (s) => {
    const active = s.conv_id === convId;
    return `<button class="session-tab${active ? " active" : ""}"
      data-conv="${esc(s.conv_id)}" ${active ? 'aria-current="page"' : ""}
      title="${esc(tabHint(s, active))}">${esc(label(s, 22))}</button>`;
  };
  strip.innerHTML = shown.map(tab).join("") + (hidden
    ? `<button class="session-tab stab-more" data-more
         title="${hidden} more session${hidden === 1 ? "" : "s"} — the whole list, with
                rename, archive and delete">+${hidden}</button>`
    : "");
  // One tab and nothing hidden is the chip said twice; the strip stays away.
  strip.classList.toggle("hidden", shown.length < 2 && !hidden);
}

export async function refreshSessionBar() {
  const chip = $("session-chip");
  const convId = store.convId;
  let sessions = [];
  try {
    sessions = await api.sessions();
  } catch { /* offline: keep whatever is on screen */ }
  if (store.convId !== convId) return;   // switched while we were asking
  const mine = sessions.find((s) => s.conv_id === convId);
  chip.textContent = label(mine, 42) + " ▾";
  chip.title = `Session ${convId} — click for the full list`;
  renderSessionTabs(sessions, convId, mine);
  onTitle(label(mine, 120));
}

const bumpSessionBar = debounce(refreshSessionBar, 800);

/** Blank the chip and the strip. They belong to the project being left; the
 *  wrong project's sessions must not sit there until the new list arrives. */
export function clearSessionBar() {
  $("session-chip").textContent = "New session ▾";
  $("session-tabs").innerHTML = "";
  $("session-tabs").classList.add("hidden");
}

/** `onPick(convId)` and `onNew()` switch conversations; `onTitle(title)`
 *  hears the open session's title each time the bar re-reads the list. */
export function initSessionBar(opts) {
  embedded = !!opts.embedded;
  onTitle = opts.onTitle || onTitle;
  // One set of hooks for both ways into the switcher — the chip's popover and
  // the strip's overflow button — so they can never come to mean two things.
  const switcher = { onPick: opts.onPick, onNew: opts.onNew };

  $("session-chip").addEventListener("click", (e) =>
    openSessionMenu(e.currentTarget, switcher));

  // The strip switches; it does not manage. Everything else a session can have
  // done to it lives one click away, in the popover the overflow button opens.
  $("session-tabs").addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (more) { openSessionMenu(more, switcher); return; }
    const tab = e.target.closest("[data-conv]");
    if (!tab || tab.dataset.conv === store.convId) return;
    opts.onPick(tab.dataset.conv);
  });

  // A rename can happen in the popover or in a Home row, and either may name
  // the session that is open; archiving and deleting happen in the popover and
  // change which sessions there are to switch to. Either way the bar re-reads
  // rather than guessing what the list now looks like.
  for (const ev of ["qc:session-renamed", "qc:sessions-changed"]) {
    window.addEventListener(ev, () => {
      if (!document.getElementById("app").classList.contains("showing-home")) {
        refreshSessionBar();
      }
    });
  }

  subscribe((kind, ev) => {
    if (kind === "event" && ev.type === "user_message") bumpSessionBar();
  });
}
