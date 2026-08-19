// The keyboard and slash-command reference, in one place.
//
// Two surfaces show this list: the `?` modal (js/modals.js), which has to be
// fast, and Help ▸ Keyboard & commands, which has room to explain. They read
// the same array, because a shortcut list that exists twice is a shortcut list
// that is wrong in one of the two places.
//
// The slash commands mirror `COMMANDS` in js/composer.js, which is what
// actually runs when you type one.

export const KEYS = [
  ["Enter", "Send the message"],
  ["Shift + Enter", "Newline inside the composer"],
  ["Esc", "Close the topmost menu or dialog — and, when none is open, interrupt the agent"],
  ["↑ / ↓", "Walk back and forward through sent-message history — filtered by "
    + "what you have already typed, so \"git\" then ↑ recalls only the messages "
    + "that start with it"],
  ["/", "Open the slash-command menu; Tab completes, Enter runs, Esc closes"],
  ["@", "Complete a file path from this project; Tab or Enter inserts it, and a "
    + "directory keeps the menu open on its contents"],
  ["Ctrl + `", "Open or close the terminal drawer at the bottom — a real shell "
    + "in this project's directory, yours to type in"],
];

export const SLASH = [
  ["/compact", "", "Compress the conversation into a summary"],
  ["/clear", "", "Start a new conversation"],
  ["/mode", "<plan|ask|auto-edit|dontask|yolo>", "Switch the permission mode"],
  ["/model", "", "Pick the model for this session"],
  ["/composition", "", "Switch this session's composition (at a turn boundary)"],
  ["/profile", "", "Switch this session's permission profile (takes effect now)"],
  ["/init", "", "Have the agent survey this project and write QUICKCODE.md"],
  ["/help", "", "The quick reference"],
];

export const PANEL_NOTE =
  "The right-hand panel holds Trajectory, Agents, Tasks, Files and Usage. Drag "
  + "its left edge to resize, press ⛶ to give it the whole window (Esc brings "
  + "the chat back), and use the ⌕ trace links in the transcript to jump "
  + "straight to an event.";

export const TERMINAL_NOTE =
  "The bottom drawer (Ctrl + `) is a real terminal in this project's "
  + "directory. Its second tab, Agent, lists every command the agent ran, "
  + "with its output as the terminal would have drawn it; ▸ run here puts one "
  + "at your own prompt without running it. The agent cannot type into your "
  + "shell and never sees what you do there — the two are separate sessions.";
