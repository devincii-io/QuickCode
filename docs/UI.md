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
| Help | `#/help/…` | How the parts fit together, the modes, permissions, shortcuts and a first-session walkthrough (`js/help/`). |

## Inside an agent pane

- **Top bar** — project and session chips, session tabs, the update chip,
  new conversation, and toggles for the side panel, the terminal, Help and
  Settings.
- **Transcript** (`js/chat.js`) — streaming markdown, reasoning, tool calls
  with their results, diffs, and system notes. Rendering is batched per
  animation frame and a streaming message patches one live node rather than
  re-rendering the transcript.
- **Composer** (`js/composer.js`) — `Enter` sends, `Shift+Enter` inserts a
  newline, `/` opens the slash menu, `@` completes a project path, `↑/↓` walks
  sent-message history filtered by what is already typed. Messages sent while
  the agent is busy are queued. Beside it: the mode pill, the model pill, the
  composition and permission-profile pickers, *compact*, quick settings,
  Stop and Send.
- **Side panel** — **Trajectory**, **Agents**, **Tasks**, **Files** and
  **Usage** tabs (`js/panel.js`, `js/panels/`). The panel can be resized or
  maximised.
- **Terminal drawer** (`Ctrl` + `` ` ``, `js/terminal/`) — a real shell in the
  project directory for *you* (`pty/interactive.py`, `server/terminal.py`),
  plus an *Agent* tab listing every command the agent ran with its output. The
  agent cannot type into this shell and never sees it; no tool can reach the
  terminal route.
- **Status bar** — state, model, context use, time, speed, cache hits,
  tokens, cost and connection.

## Trajectory

The Trajectory view (`js/trajectory.js`) renders the session event log as an
inspectable table: role chips and filters, a timeline strip with gap
compression and live follow, search, export, and a per-event inspector with
**Summary / Payload / Result / Timing** tabs. The ⌕ links in the transcript jump
to the matching event. It reads the same stream that resume and replay use, so
what it shows is what the model saw — see docs/ARCHITECTURE.md for which event
types are logged.

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

- **Permission prompt** (`js/modals.js`) — shows the tool and the call's own
  preview (for `bash`, the command itself), then **Allow once**, **Always
  allow** (shows the exact rule and the file it goes to) and **Deny** (a
  second click confirms, with an optional message returned to the model).
  Details in docs/PERMISSIONS.md §The prompt.
- **Plan review** — the plan as markdown, with **Approve · auto-edit**,
  **Approve · ask mode** and **Keep planning** (feedback returns to the
  model).
- **Project trust** (`js/trust.js`) — shown when a project's own files name
  programs to run (`mcpServers`, `kind: tool` plugins) or widen permissions;
  lists every command line before you grant it.

## Keyboard

The authoritative list is `KEYS` and `SLASH` in `js/help/shortcuts.js`, which
both the `?` quick reference and Help ▸ Keyboard read.

| Key | Action |
|---|---|
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
