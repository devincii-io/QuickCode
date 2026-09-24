// The command palette inside an agent pane: this conversation, this pane and
// this project's pages. js/palette.js draws it; this decides what is in it.

import { api } from "./api.js";
import { SLASH_COMMANDS } from "./composer/commands.js";
import { runSlash } from "./composer/slash.js";
import { openHelp } from "./help/quickref.js";
import { SECTIONS } from "./help/view.js";
import { openModelMenu } from "./menus.js";
import { MODES } from "./modes.js";
import { togglePanel } from "./panel.js";
import { SETTINGS_PAGES, bindPaletteKey, openPalette } from "./palette.js";
import { store } from "./store.js";
import { toggleTerminal } from "./terminal/panel.js";
import { highlightHtml } from "./text_match.js";
import { oneLine, relTime } from "./util.js";
import { actions } from "./ws.js";

const WHO = { user: "you", assistant: "agent", tool: "tool" };
const $ = (id) => document.getElementById(id);

/** `tell(action, data)` reaches the workspace shell (null outside one);
 *  `openConversation(convId, { seq })` is main.js's. */
export function initPanePalette({ tell = null, openConversation }) {
  bindPaletteKey(() => openPalette(build({ tell, openConversation })));
}

function build({ tell, openConversation }) {
  const live = !!store.convId && !$("app").classList.contains("showing-home");
  const items = [];
  const add = (group, title, run, more = {}) => items.push({ group, title, run, ...more });

  if (live) {
    for (const c of SLASH_COMMANDS) {
      add("Slash commands", c.name, () => runSlash(c.name), { hint: c.desc, keywords: "slash command" });
    }
    const mode = store.state?.mode;
    for (const m of MODES) {
      if (m.id === "yolo" && !store.bootstrap?.allow_yolo) continue;
      add("Permission mode", `Switch to ${m.title.toLowerCase()}`, () => actions.setMode(m.id),
        { hint: m.summary, keywords: `mode ${m.id}`, current: mode === m.id });
    }
    add("Model", "Choose a model…", () => openModelMenu($("model-pill")),
      { hint: store.state?.model ? `Now: ${store.state.model}` : "", keywords: "switch model llm" });
    add("Sessions", "New conversation", () => openConversation(null), { keywords: "clear chat session" });
  }
  if (tell) {
    const key = (k) => () => tell("shortcut", { key: k, altKey: true });
    // `leaves`: the new pane or the shell's palette takes the focus from here.
    add("Panes", "New agent pane", key("n"), { keys: "Alt+N", keywords: "open split", leaves: true });
    add("Panes", "Split right", () => tell("split", { dir: "h" }), { keywords: "pane new agent", leaves: true });
    add("Panes", "Split below", () => tell("split", { dir: "v" }), { keywords: "pane new agent", leaves: true });
    add("Panes", "Maximize or restore this pane", key("z"), { keys: "Alt+Z", keywords: "zoom" });
    add("Panes", "Show or hide the workspace sidebar", key("b"), { keys: "Alt+B" });
    add("Panes", "Go to another agent or workspace…", () => tell("palette"),
      { keywords: "switch focus", leaves: true });
  }
  if (live) {
    add("View", "Show or hide the side panel", () => togglePanel(),
      { keywords: "trajectory agents tasks files usage" });
    add("View", "Show or hide the terminal", () => toggleTerminal(), { keys: "Ctrl+`", keywords: "shell" });
  }
  for (const [route, title] of SETTINGS_PAGES) {
    add("Settings", title, () => { location.hash = route; }, { keywords: "settings configuration" });
  }
  add("Help", "Keyboard shortcuts and slash commands", () => openHelp(), { keywords: "help keys ?" });
  for (const s of SECTIONS) {
    add("Help", s.title, () => { location.hash = `#/help/${s.slug}`; }, { hint: s.blurb, keywords: "help" });
  }

  return {
    items,
    placeholder: live ? "Type a command, a page, or words from a past conversation…"
      : "Type a command or a page…",
    load: live ? recentSessions(openConversation) : null,
    search: live ? (q) => messageHits(q, openConversation) : null,
    searchGroup: "In past conversations",
  };
}

async function recentSessions(openConversation) {
  const list = await api.sessions();
  return list
    .filter((s) => s.conv_id !== store.convId && s.message_count)
    .slice(0, 20)
    .map((s) => ({
      group: "Sessions", title: `Open ${oneLine(s.title, 80)}`, hint: relTime(s.mtime),
      keywords: "session conversation", run: () => openConversation(s.conv_id),
    }));
}

async function messageHits(q, openConversation) {
  const answer = await api.searchSessions(q, { limit: 15 });
  return answer.results.flatMap((r) => {
    const title = oneLine(r.title, 80);
    if (!r.hits.length) {
      return [{ title, hint: "The title matches", run: () => openConversation(r.conv_id) }];
    }
    return r.hits.map((h) => ({
      title,
      hintHtml: `<span class="mi-who">${WHO[h.where] || "?"}</span> ${highlightHtml(h.snippet, answer.terms)}`,
      run: () => openConversation(r.conv_id, { seq: h.seq }),
    }));
  }).slice(0, 25);
}
