// The Help pages, in rail order, as data only: the rail, the router and both
// command palettes read this list, and a palette in the workspace shell must
// not load every page's renderer (and the configuration view behind them) to
// learn a page's title. help/view.js adds the renderers.

export const HELP_PAGES = [
  { slug: "workspaces", title: "Workspaces & panes", sigil: "◫",
    blurb: "Arrange agents and customize your workspace." },
  { slug: "overview", title: "The big picture", sigil: "◎",
    blurb: "How one message becomes an answer." },
  { slug: "plugins", title: "The plugin model", sigil: "::",
    blurb: "What everything in Settings is." },
  { slug: "questions", title: "The six questions", sigil: "¶",
    blurb: "How to read any card in Settings." },
  { slug: "permissions", title: "Permissions & trust", sigil: "§",
    blurb: "What it may do, and what it may run." },
  { slug: "tutorial", title: "Your first session", sigil: "1.",
    blurb: "A walkthrough, in order." },
  { slug: "handson", title: "Hands-on", sigil: "fn",
    blurb: "Three things you can try here." },
  { slug: "keyboard", title: "Keyboard & commands", sigil: "[]",
    blurb: "Shortcuts and slash commands." },
];
