// Slash commands: what each one does, the entries the composer's menu offers
// for what is typed, and running one that is typed in full.

import { api } from "../api.js";
import { openHelp } from "../help/quickref.js";
import { openModeMenu, openModelMenu } from "../menus.js";
import { MODE_IDS, MODES } from "../modes.js";
import { toast } from "../toast.js";
import { actions } from "../ws.js";
import { openCompositionMenu, openProfileMenu } from "./pills.js";

const $ = (id) => document.getElementById(id);

let hooks = { onNewConversation: () => {} };

export function initSlash(h) {
  hooks = { onNewConversation: () => {}, ...(h || {}) };
}

// ---- /init: the project-instructions file, written by the agent ----
//
// `_load_project_instructions` (quickcode/config.py) reads the first of
// QUICKCODE.md, AGENTS.md, CLAUDE.md it finds at the project root and injects
// it verbatim into every prompt. There is deliberately no endpoint that writes
// a file at the project root, and this command does not add one: it sends a
// message. The agent surveys the repository with the tools it already has and
// writes the file with `write`, which means the permission engine sees the
// write like any other — a slash command that could put a file on disk without
// passing the gate would be a hole in the gate.

const INSTRUCTION_FILES = ["QUICKCODE.md", "AGENTS.md", "CLAUDE.md"];

const INIT_PROMPT = `Write a QUICKCODE.md at the root of this project.

QuickCode injects that file verbatim into the system prompt of every session \
here, so it is read by someone who has never seen this repository and is about \
to change it.

Survey before you write. Read the dependency and build manifests, the test \
configuration, the CI workflow, the README, and enough of the source tree to \
see how it is actually laid out. Where the documentation and the code \
disagree, the code is right.

Then write the file with the write tool. Cover:

- Build, run and test: the exact commands, copied from the manifests or CI \
rather than guessed, and which one to run to check a change.
- Architecture: the few directories or modules that matter, what each is \
responsible for, and where a request or a command actually enters.
- Conventions this repository keeps that a newcomer would otherwise break — \
naming, formatting, imports, error handling, test style, commit style.
- Anything genuinely surprising: a rule you had to read the source to discover.

Keep it short. One screen, ideally under 60 lines — it is a prompt paid for on \
every turn, not documentation. No preamble, no history, no praise for the \
project. Leave a section out rather than filling it with a plausible guess, \
and say at the end what you left out and why.`;

const initUpdatePrompt = (name) => `This project already has a ${name} at its \
root, and QuickCode reads it into the prompt of every session here. Update it \
— do not overwrite it.

Read it first, then check it against the repository as it is now: try the \
build and test commands it names, look at the directories it describes, and \
confirm the conventions it claims. Find what has gone stale and what is \
missing.

Then edit it in place, keeping its structure and its voice, and tell me what \
you changed and why. Leave every line that is still accurate alone. It should \
stay short — one screen — and it should still cover the build, run and test \
commands, the architecture worth knowing, and the conventions a newcomer would \
otherwise break.`;

// Which of the three names is actually on disk. Asked one name at a time so
// the answer is exact: a root listing can be capped, and "is QUICKCODE.md
// there" must not depend on how many files sort ahead of it.
async function existingInstructions() {
  const found = [];
  for (const name of INSTRUCTION_FILES) {
    try {
      const res = await api.paths(name, 5);
      if ((res.paths || []).some((p) => !p.is_dir && p.path.toLowerCase() === name.toLowerCase())) {
        found.push(name);
      }
    } catch { /* offline or no project: fall through to the write prompt */ }
  }
  return found;
}

async function runInit() {
  const found = await existingInstructions();
  if (found.length) {
    toast(`${found[0]} already exists — asking the agent to update it rather `
      + "than replace it.", { kind: "info" });
    actions.userMessage(initUpdatePrompt(found[0]));
    return;
  }
  actions.userMessage(INIT_PROMPT);
}

// ---- slash commands ----
// Each entry: { label, arg, desc, complete, exec }. `exec` missing means the
// entry only completes text (e.g. "/mode " opens the mode sub-entries).

export const COMMANDS = [
  {
    name: "/compact", desc: "Compress the conversation into a summary",
    exec: () => actions.compact(),
  },
  {
    name: "/clear", desc: "Start a new conversation",
    exec: () => hooks.onNewConversation(),
  },
  {
    name: "/mode", arg: `<${MODE_IDS.join("|")}>`,
    desc: "Switch the permission mode",
    complete: "/mode ",
    // no exec: Tab/Enter completes to "/mode " and lists the modes
    fallback: () => openModeMenu($("mode-pill")),
  },
  {
    name: "/model", desc: "Pick the model for this session",
    exec: () => openModelMenu($("model-pill")),
  },
  {
    name: "/composition", desc: "Switch this session's composition (at a turn boundary)",
    exec: () => openCompositionMenu($("composition-pill") || $("model-pill")),
  },
  {
    name: "/profile", desc: "Switch this session's permission profile (takes effect now)",
    exec: () => openProfileMenu($("profile-pill") || $("mode-pill")),
  },
  {
    name: "/init", desc: "Have the agent survey this project and write QUICKCODE.md",
    exec: () => { runInit(); },
  },
  {
    name: "/help", desc: "Keyboard shortcuts and slash commands",
    exec: () => openHelp(),
  },
];

// Entries the menu should show for the current input text, or null for
// "no menu" (the text is not a bare slash command).
export function entriesFor(text) {
  if (!text.startsWith("/")) return null;

  const modeMatch = /^\/mode\s+(.*)$/.exec(text);
  if (modeMatch) {
    const q = modeMatch[1].trim().toLowerCase();
    return MODES
      .filter(({ id }) => id.startsWith(q))
      .map(({ id, summary }) => ({
        label: "/mode " + id, desc: summary, complete: "/mode " + id,
        exec: () => actions.setMode(id),
      }));
  }

  if (/\s/.test(text)) return null;   // an argument for something else — no menu
  const q = text.toLowerCase();
  return COMMANDS
    .filter((c) => c.name.startsWith(q))
    .map((c) => ({
      label: c.name, arg: c.arg, desc: c.desc,
      complete: c.complete || c.name, exec: c.exec,
    }));
}

// Run a fully typed slash command. Three answers, not two: `false` means the
// text is not a command at all (so it goes as a message), `"refused"` means it
// is one but the socket could not carry it — and the composer keeps the text
// rather than clearing a box over a command that never happened — and `true`
// means it ran. `actions.*` return false exactly when the send was refused, so
// the distinction costs nothing to propagate.
export function runSlash(text) {
  const parts = text.split(/\s+/);
  const c = COMMANDS.find((x) => x.name === parts[0]);
  if (!c) return false;
  const arg = parts.slice(1).join(" ").trim();
  if (c.name === "/mode") {
    if (!arg) { c.fallback(); return true; }
    return actions.setMode(arg) || "refused";
  }
  if (!c.exec) return false;
  return c.exec() === false ? "refused" : true;
}
