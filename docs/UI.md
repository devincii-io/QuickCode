# The web UI

QuickCode's UI is plain ES modules under `quickcode/frontend/` — no bundler, no
build step — served by the loopback FastAPI server and shown in a pywebview
window (`quickcode/ui/window.py`) or, when that is unavailable, the default
browser. The design goal is unchanged from the retired terminal UI
(`docs/archive/UI-TEXTUAL.md`): everything the agents do is visible, and nothing
they do blocks the interface.

## Two layers: the workspace shell and agent panes

`js/entry.js` decides which of two programs a page runs:

- **The workspace shell** (`js/workspaces.js`) is the outer window. It owns the
  sidebar of workspaces (one per open project folder), the split tree of agent
  panes (`js/split_tree.js`, adapted from QuickTerm), and the saved layout
  (`js/workspace_state.js`, at most `MAX_PANES` panes per workspace). It never
  talks to a conversation itself.
- **An agent pane** is an iframe loaded with `?pane=1`, which runs `js/main.js`
  with its own store, WebSocket, composer and review dialogs — one conversation
  per pane. Panes are positioned with absolute boxes and never reparented,
  because moving a mounted iframe's DOM node reloads it. Messages between the
  shell and a pane check both the origin and the exact source window.

Closing a pane detaches its view and keeps the conversation (and a running
agent) alive; **Stop** is the explicit way to interrupt. *Reopen closed pane*
restores the last one. Each pane's header and its sidebar entry show a status —
`Ready`, `Working`, `Needs approval` or `Offline` — so an agent waiting on a
permission prompt in a pane you are not looking at is still visible.

### Notifications

A pane you are not looking at tells you when its agent **finished a turn**,
**needs approval** (a permission prompt or a plan review) or **stopped with an
error**. The pane only reports what happened (`js/pane_notices.js`, over the
same origin- and source-checked bridge as everything else); the shell decides
whether you saw it, because only the shell knows which pane is focused and
whether the window is in front (`js/notify.js`). A notice for the focused pane
of the visible workspace, while the window has focus, is simply not raised.
Anything else becomes:

- a **count badge** on the pane header, on its agent row in the sidebar and,
  summed, on its workspace row — amber for a waiting review, red for an error,
  green for a finished turn, most urgent first;
- a **title badge**, `(2) Website redesign | QuickCode`, the total unseen;
- a **desktop notification**, only while the window is in the background and
  only once *Desktop notifications* is switched on under Appearance (the
  sidebar's *Appearance*, or Settings ▸ Appearance). It is off by default, and
  the browser is asked for permission by that toggle and never otherwise.
  Notifications are silent and carry the agent's name (the session title the
  sidebar shows) and the kind of event — for a permission prompt, the tool's
  name — but never a message, a command or an error text, which are not for
  a lock screen. Clicking one brings the window forward on that pane. Where the window
  has no Notification API — the native window's WebView may not — the toggle
  is disabled with a note, and the badges carry on alone.

Focusing a pane (clicking it, its sidebar row, `Alt+arrow`) marks it seen, and
so does returning to the window on the pane you left. Replayed history never
raises anything, and neither does the end of a turn you interrupted yourself.
A pane opened on its own, outside the workspace, keeps its own title badge. The
badges' entrance animation follows the *Animate activity indicators* setting
and the system's reduced-motion preference; nothing makes a sound.

Storage split: the auth token stays in the tab's `sessionStorage`; workspace
`localStorage` holds only names, session ids and layout. Unsent drafts survive
pane closure and a reload within the same tab. Appearance preferences
(`js/appearance.js`) are shared across panes through storage events, so a theme
change reaches every open pane without a reload.

## Views

| View | Route | What it is |
|---|---|---|
| Home | — | Recent projects (`~/.quickcode/projects.json`), open a folder, remove or purge a project. |
| Workspace | — | The shell above: sidebar, toolbar (*New agent*, *Reopen closed pane*), pane grid. |
| Configuration | `#/config/…` | Application settings (provider, appearance, models, web search, updates), then agents, compositions, permission profiles, hooks, parts, your authored plugins, and the machine room. Built from the live plugin kernel (`js/config/`). **Hooks** (`#/config/hooks`, `js/config/hooks.js`) adds, edits, deletes and test-runs command hooks — docs/HOOKS.md §The Hooks page. |
| Help | `#/help/…` | How the parts fit together, the modes, permissions, shortcuts and a first-session walkthrough (`js/help/`). The Hands-on permission sandbox, like the profile editor's preview, asks the project's real engine (`POST …/permissions/explain`, rendered by `js/help/explain.js`; docs/PERMISSIONS.md §Why was I prompted?). |

## Inside an agent pane

- **Top bar** — project and session chips (the session chip's list is also
  where sessions are searched — see [Finding a conversation](#finding-a-conversation)),
  session tabs (`js/sessionbar.js`),
  the update chip, new conversation, and toggles for the side panel, the
  terminal, Help and Settings.
- **Transcript** (`js/chat.js`, `js/chat/`) — streaming markdown, reasoning,
  tool calls with their results, diffs, and system notes. A streaming message
  patches one live node (`chat/stream.js`) at most once a frame; tool cards and
  their permission badges are built in `chat/cards.js`; results, verdicts and
  subagent steps find their card by id (`chat/registry.js`), never by querying
  the page. Following the newest line is decided by where the reader scrolled
  and applied once a frame (`chat/scroll.js`). Long sessions are windowed
  (`chat/window.js`): a replay is built off-document and only the newest 120
  blocks are attached; scrolling to the top, or *show N earlier items*, brings
  back 80 more at a time without moving the page. `scripts/bench_chat.js`
  times a 10k-event replay and live events against the workspace smoke server.
  A user message whose turn changed files carries **↺ Rewind files**
  (`chat/rewind.js`, found by turn number like a card by its id), and a rewind
  leaves a note where it happened.
- **Composer** (`js/composer.js`, with its commands, `@` completion, recall
  and pills in `js/composer/`) — `Enter` sends, `Shift+Enter` inserts a
  newline, `/` opens the slash menu, `@` completes a project path, `↑/↓` walks
  sent-message history filtered by what is already typed. Messages sent while
  the agent is busy are queued. Beside it: the mode pill, the model pill, the
  composition and permission-profile pickers, *compact*, quick settings,
  Stop and Send.
- **Side panel** — **Trajectory**, **Agents**, **Tasks**, **Files**,
  **Checkpoints** and **Usage** tabs (`js/panel.js`, `js/panels/`). The panel
  can be resized or maximised.
- **Terminal drawer** (`Ctrl` + `` ` ``, `js/terminal/`) — a real shell in the
  project directory for *you* (`pty/interactive.py`, `server/terminal.py`),
  plus an *Agent* tab listing every command the agent ran with its output. The
  agent cannot type into this shell and never sees it; no tool can reach the
  terminal route.
- **Jobs** (a third tab in the terminal drawer, `js/terminal/jobs.js`) — the
  commands the agent started with `run_in_background`: each with a status chip
  (`running`, `exit 0`, `exit 1`, `killed`, `killed by you`), its duration and
  output size, and for the one selected, its output tailing live through the
  same terminal renderer as the Agent tab — colour and progress-bar redraws
  intact, the newest 1,500 lines on the page. *Copy command* copies it; *Kill*
  asks first, then stops the whole process tree; the transcript notes it and
  the agent is told at the start of its next turn. Reading here never marks
  output as read for the agent (the meta line says how much it has not read).
  It lives in the drawer rather than the side panel because a job's output is
  a terminal log, as wide as the shell's, and it continues the Agent tab: that
  one lists commands that ran to completion, this one those left running. The
  drawer's toggle and the tab carry a count while any job runs.
  Behind it: `server/jobs_api.py` (docs/TOOLS.md §bash). `scripts/smoke_jobs.js`
  drives it in a browser against `scripts/workspace_smoke_server.py --jobs`,
  whose preview agent starts a ticking background job.
- **Status bar** (`js/statusbar.js`) — state, model, context use, time, speed,
  cache hits, tokens, cost and connection. A connection that stays down past
  a moment gets a banner as well (`js/connbanner.js`).

## Trajectory

The Trajectory view (`js/trajectory.js`) renders the session event log as an
inspectable table: role chips and filters, a timeline strip with gap
compression and live follow, search, export, and a per-event inspector with
**Summary / Payload / Result / Timing** tabs. The ⌕ links in the transcript jump
to the matching event. It reads the same stream that resume and replay use, so
what it shows is what the model saw — see docs/ARCHITECTURE.md for which event
types are logged.

## Finding a conversation

The session chip's list (`js/sessions_menu.js`) opens with a search box.
Typing filters the rows by title at once; after a short pause the server
searches the messages too (`GET …/sessions/search?q=`,
`session/search.py`) and lists the matches under *In messages*, each with a
highlighted snippet. What is searched: titles, what you typed, what the agent
answered, and the names of the tools it called — not tool output, and not the
system prompt. Terms are case-insensitive and all of them must occur in the
same message; `"a quoted phrase"` counts as one term. There are no regular
expressions, by design: a pattern that backtracks for ever has no timeout.

A search reads the newest sessions first and stops at 30 matching sessions,
256 MiB of log or 1.5 s, whichever comes first, and says so under the results.
It runs on the server's thread pool, so it never holds up a streaming turn.
The archive is searched too; opening an archived session restores it to the
list, as it always has.

Clicking a match opens that conversation at the matching event, in the
inspector (`js/inspect.js`) — the same view a ⌕ link opens. Inside the
workspace a conversation that already has a pane is focused rather than opened
twice; otherwise a new pane opens with the event's `seq` in its URL (`at=`),
which is never saved in the layout. `↑`/`↓` walk the list, `Enter` opens the
first entry, and `Esc` clears the search before it closes the list.

## The event protocol

Each pane opens one WebSocket, `/ws/projects/{pid}/conversation/{conv_id}`,
carrying the loopback token as a `qcauth.<token>` subprotocol (browsers cannot
set headers on a WebSocket). On attach the server sends the conversation's
`state`, then `replay_start`, every logged event, and `replay_done`; after that
the stream is live. Events carry a `seq`, and the store dedupes by it, so a
reconnect simply replays from scratch. `js/ws.js` allows exactly one socket per
pane to reach the store — a generation counter makes a superseded socket deaf
and mute. A client that falls behind is disconnected by the server (a bounded
queue, never an unbounded buffer) and reconnects to replay. The server sends a
heartbeat every 15 s so silence means the connection is gone.

Client → server frames are `user_message`, `interrupt`, `set_mode`,
`set_model`, `compact`, `permission_decision` and `plan_decision`
(`server/ws.py::_dispatch`).

## Dialogs the agent waits on

- **Permission prompt** (`js/reviews.js`) — shows the tool and the call's own
  preview (for `bash`, the command itself); a hook's reason when a hook raised
  it; for `edit` and `write` the diff the call would make (drawn by `js/diff.js`,
  which the transcript's edit cards use too); the exact rules **Always allow**
  would save and the parts that would still ask (`js/permission_offer.js`); and
  *Why am I being asked?*, the engine's own explanation inline
  (`js/help/explain.js`). Then **Allow once**, **Always allow** (greyed out when
  there is nothing to save) and **Deny** (a second click confirms, with an
  optional message returned to the model). The buttons ignore clicks for
  400 ms after a prompt appears. Details in docs/PERMISSIONS.md §The prompt.
- **Plan review** — the plan as markdown, with **Approve · auto-edit**,
  **Approve · ask mode** and **Keep planning** (feedback returns to the
  model).
- **Project trust** (`js/trust.js`) — shown when a project's own files name
  programs to run (`mcpServers`, `kind: tool` plugins) or widen permissions;
  lists every command line before you grant it.

## Rewinding files

**Rewind files** under a user message, or **Rewind…** on a turn in the
Checkpoints tab, opens `js/checkpoints/dialog.js` on the
preview of putting files back to before that turn: each file with what would
happen to it, its `+`/`−` counts and an expandable diff, a checkbox per file,
conflicts in red behind an explicit **Overwrite anyway**, and the notice that
bash changes are not tracked. It is refused, with the server's reason, while
a turn runs, a prompt waits or a background job works. The rules it follows
live in `js/checkpoints/model.js`. The whole of it:
docs/CHECKPOINTS.md §In the app.

## Command palette

`Ctrl+K` (`⌘K` on macOS — and only that there, since `Ctrl+K` in a macOS
text field deletes to the end of the line) opens a searchable list of
everything you can do from where you are (`js/palette.js`). Type to filter — every word must match, the
start of a title ranks first, then the start of a word in it, then a word in
its description — `↑`/`↓` (or `PgUp`/`PgDn`) choose, `Enter` runs, `Esc` or
`Ctrl+K` again closes. It is a `role="dialog"` holding a combobox and a
listbox, with the highlighted option tracked by `aria-activedescendant`.

The key opens the palette of the document that has focus, so there are two:

- **In an agent pane** (`js/palette_pane.js`): every slash command, read from
  the same table the composer runs them from (`js/composer/commands.js`); each
  permission mode; the model picker; *New conversation* and the recent
  sessions; the pane commands (new pane, split right or below, maximize, the
  sidebar, and *Go to another agent or workspace…*, which hands over to the
  shell's palette); the side panel and the terminal; every Settings page; and
  Help. Past conversations are searched too: after a pause, messages that
  match are listed under *In past conversations* — the same search as
  [Finding a conversation](#finding-a-conversation), opening the session at
  the matching event.
- **In the workspace shell** (`js/workspace_palette.js`), when focus is in the
  sidebar or on Home: new pane, split, maximize, reopen closed pane, the
  sidebar, every open agent and workspace, known projects not yet open, *Open
  folder…*, Appearance, and the Settings and Help pages (in the utility
  dialog).

A command that moves focus keeps it; one that does not (a mode switch,
*Show or hide the terminal*) gives it back to where it was, normally the
composer. The palette does not open over a dialog, not in the Settings/Help
dialog's own frame, and not in the terminal, whose shell keeps `Ctrl+K`.

## Keyboard

The authoritative lists are `KEYS` in `js/help/shortcuts.js` and the slash
commands in `js/composer/commands.js` (which `slashRows()` in shortcuts.js
reads), and both the `?` quick reference and Help ▸ Keyboard show them.

| Key | Action |
|---|---|
| `Ctrl+K` (`⌘K` on macOS) | Command palette |
| `Alt+N` | New agent pane in this workspace |
| `Alt+Z` | Maximise the focused pane, or restore the layout |
| `Alt+B` | Show or hide the workspace sidebar |
| `Alt+arrow keys` | Move focus between panes |
| `Enter` / `Shift+Enter` | Send / newline |
| `Esc` | Close the topmost menu or dialog; otherwise interrupt the agent |
| `↑` / `↓` | Sent-message history, prefix-filtered |
| `/` | Slash-command menu: `/compact`, `/clear`, `/mode`, `/model`, `/composition`, `/profile`, `/init`, `/help` |
| `@` | Path completion |
| `Ctrl` + `` ` `` | Toggle the terminal drawer |

There is no key binding for cycling the permission mode: use the mode pill or
`/mode <name>` (docs/PERMISSIONS.md §Modes).
