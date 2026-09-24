// The command palette in the workspace shell: panes, agents, workspaces and
// pages. A plain function of the shell's state; `act` is what each entry calls.
// Conversation commands live in each pane's own palette (js/palette_pane.js).

import { SETTINGS_PAGES } from "./palette.js";

/** `state`: { home, active, zoomed, undo, workspaces: [{ id, name, ready,
 *  focused, panes: [{ id, title, state }] }] }; `help`: [{ slug, title, blurb }]. */
export function workspaceItems(state, act, help = []) {
  const items = [];
  const add = (group, title, run, more = {}) => items.push({ group, title, run, ...more });
  const here = state.home ? null : state.workspaces.find((w) => w.id === state.active);

  if (here?.ready) {
    add("Workspace", "New agent pane", act.newAgent, { keys: "Alt+N", keywords: "open" });
    if (here.focused) {
      add("Workspace", "Split right", () => act.split("h"), { keywords: "pane new agent" });
      add("Workspace", "Split below", () => act.split("v"), { keywords: "pane new agent" });
      add("Workspace", state.zoomed ? "Restore the pane layout" : "Maximize the focused pane",
        act.zoom, { keys: "Alt+Z", keywords: "zoom" });
    }
    if (state.undo) add("Workspace", "Reopen closed pane", act.reopen, { keywords: "undo restore" });
  }
  add("Workspace", "Show or hide the sidebar", act.sidebar, { keys: "Alt+B" });
  for (const w of state.workspaces) {
    for (const p of w.panes) {
      add("Agents", p.title, () => act.focusPane(w.id, p.id), {
        hint: [w.name, p.state].filter(Boolean).join(" · "), keywords: "agent pane go to",
        current: !state.home && w.id === state.active && w.focused === p.id,
      });
    }
  }
  for (const w of state.workspaces) {
    add("Workspaces", `Switch to ${w.name}`, () => act.openWorkspace(w.id),
      { current: !state.home && w.id === state.active, keywords: "workspace project" });
  }
  add("Projects", "Open folder…", act.openFolder, { keywords: "project new" });
  add("Projects", "All projects", act.projects, { keywords: "home recent" });
  add("Appearance", "Workspace appearance…", act.appearance, { keywords: "theme font notifications" });
  for (const [route, title] of SETTINGS_PAGES) {
    add("Settings", title, () => act.route(route), { keywords: "settings configuration" });
  }
  for (const s of help) {
    add("Help", s.title, () => act.route(`#/help/${s.slug}`), { hint: s.blurb, keywords: "help" });
  }
  return items;
}
