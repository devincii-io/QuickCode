// The slash commands, described once. What each one does when run is attached
// in ./slash.js; the composer's menu, the command palette and both help
// surfaces (js/help/shortcuts.js) read their names and descriptions from here,
// so a command cannot be listed in one place and missing from another.

import { MODE_IDS } from "../modes.js";

export const SLASH_COMMANDS = [
  { name: "/compact", desc: "Compress the conversation into a summary" },
  { name: "/clear", desc: "Start a new conversation" },
  { name: "/mode", arg: `<${MODE_IDS.join("|")}>`, desc: "Switch the permission mode" },
  { name: "/model", desc: "Pick the model for this session" },
  { name: "/composition", desc: "Switch this session's composition (at a turn boundary)" },
  { name: "/profile", desc: "Switch this session's permission profile (takes effect now)" },
  { name: "/init", desc: "Have the agent survey this project and write QUICKCODE.md" },
  { name: "/help", desc: "Keyboard shortcuts and slash commands" },
];
