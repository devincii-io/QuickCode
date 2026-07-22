# UI Design — an app, not a log file

Textual gives us mouse support, focusable widgets, modal screens, a command palette, and CSS-like theming (TCSS). The design goal: everything visible is *live* and *clickable*, and nothing the agents do ever blocks the UI.

## Screen layout

```
┌ 1:fix-pagination ● 2:auth-refactor  3:research ○ ────────────── tabs ┐
├──────────────────────────────────────────────┬───────────────────────┤
│ MAIN TRANSCRIPT (focused conversation)       │ SIDEBAR (Ctrl+B)      │
│                                              │ ┌─ tasks ───────────┐ │
│ ⏺ Read quickcode/core/loop.py (210 lines) ▸  │ │ ✓ locate bug      │ │
│ ⏺ Edit quickcode/core/loop.py            ▾   │ │ ◐ fix off-by-one  │ │
│   - end = start + size + 1                   │ │ ○ run tests       │ │
│   + end = start + size                       │ ├─ team ────────────┤ │
│ Fixed the boundary; running tests now.       │ │ ● main    opus    │ │
│ ● bash: uv run pytest -q  [12s]              │ │ ◐ search1 sonnet  │ │
│                                              │ │ ◐ search2 sonnet  │ │
├─ agents ─────────────────────────────────────┤ ├─ files ───────────┤ │
│ ◐ search1  grepping providers/…       [1.2k] │ │ M core/loop.py    │ │
│ ◐ search2  reading docs/…             [0.8k] │ └───────────────────┘ │
├──────────────────────────────────────────────┴───────────────────────┤
│ > _                                                                  │
├──────────────────────────────────────────────────────────────────────┤
│ opus-4.8 · ctx 34% · $0.41 · MODE:ask · 2 agents · Esc stop  F1 help │
└──────────────────────────────────────────────────────────────────────┘
```

Regions:

1. **Conversation tabs** — one per conversation. `●` = background activity since last viewed, `○` = idle. Click or `Ctrl+1..9` to switch; `Ctrl+O` opens the fuzzy **conversation switcher** (titles auto-generated from the first user message; shows last-activity preview + cost). `Ctrl+N` = new conversation. Every conversation keeps running when unfocused.
2. **Main transcript** — the focused agent's stream (details below).
3. **Agent strip / panes** — live subagents render as compact rows (spinner, name, current action, token count). `Enter`/click on a row expands it to a **split pane** with the subagent's full transcript; focus follows. Once 3+ agents sit idle simultaneously they collapse into one `N idle agents` row (expand on demand) — a big fan-out never floods the strip; working/blocked/failed agents always keep their own row. Completed background agents stay listed (dimmed, below running work) so results are inspectable after the fact. The input box always routes to the *focused* agent — so you can steer a subagent or message a teammate directly. `Ctrl+F` zooms the focused pane fullscreen; again to restore.
4. **Sidebar** (`Ctrl+B`) — task board (live checkboxes, click to expand a task's detail/owner), team roster (agent name · model · status · cost), changed-files list (click → diff modal).
5. **Input** — multiline `TextArea`. `Enter` submits, `Ctrl+J` inserts newline (Shift+Enter where the terminal supports the kitty keyboard protocol — QuickTerm does). `/` opens the slash-command menu inline; `@` opens fuzzy path completion; `↑/↓` walks input history when the editor is empty.
6. **Status bar** — model · context % · session cost · **permission mode** (color-coded) · running-agent count · key hints.

## Transcript widgets

| Content | Widget behavior |
|---|---|
| Assistant text | Streaming markdown (Textual `MarkdownStream` append — no full re-renders); syntax-highlighted code blocks |
| Reasoning | Dim, collapsed to one line by default; click/`Enter` to expand |
| Tool call | One-line header (`⏺ grep "Provider" quickcode/ · 14 matches`) + collapsible body. Auto-collapsed when successful, auto-expanded on error |
| Edits | Colored unified diff; click header → full-file diff modal |
| Bash | Live-tailing output panel (PTY ring buffer) while running, collapses to head+tail on finish; `[12s]` live timer |
| `file:line` references | Clickable links → open in `$EDITOR` (VS Code `code -g file:line` when detected) |
| Interrupts/errors | Distinct colored system rows |

Rendering discipline (QuickTerm lessons): pane subscribers pull from **bounded queues** and batch-apply once per frame; overflow triggers a clean resync from the transcript store. A runaway `bash` cannot freeze the app.

## Modal screens (`push_screen_wait` — the agent loop awaits the answer)

- **PermissionModal** — what the agent wants (command / diff preview rendered inline), buttons: `Allow once` · `Always allow` (persists a rule; shows the exact rule it will write) · `Deny…` (free-text reason returned to the model). Keyboard: `y` / `a` / `n`.
- **PlanReviewModal** — the plan as rendered markdown; `Approve` · `Approve + auto-edit` (drops to auto-edit mode for execution) · `Reject with feedback` (text box).
- **ModelPicker** (`F2` or `/model`) — fuzzy search over the provider's live model list; shows pricing/context per model; separate defaults for *orchestrator* and *worker* roles.
- **ConversationSwitcher** (`Ctrl+O`) — fuzzy list, `Enter` switch, `Ctrl+D` archive.
- **Command palette** (`Ctrl+P`, Textual built-in) — every slash command + UI action, fuzzy-searchable.

## Permission modes in the UI

`Shift+Tab` cycles modes (muscle memory from Claude Code): `plan → ask → auto-edit → yolo`. The status bar segment is color-coded — plan = blue, ask = neutral, auto-edit = yellow, yolo = red background. Yolo additionally requires a confirmation on entry. Mode is per-conversation.

## Toasts & attention

Textual `notify()` for: background bash finished, subagent completed (with one-line result), compaction ran, teammate blocked on a question. A blocked agent (waiting on permission or a question) turns its tab/strip row **orange** — the UI never silently deadlocks on an invisible prompt.

## Keybinding map (QuickTerm-safe: Ctrl/F-keys only, no Alt)

QuickCode runs inside QuickTerm, which claims Alt+K/Z/W/arrows and the Alt+Shift namespace — QuickCode stays entirely off Alt.

| Key | Action |
|---|---|
| `Esc` | Interrupt focused agent (double-Esc: interrupt all) |
| `Ctrl+C` | Clear input / double-tap exit |
| `Enter` / `Ctrl+J` | Submit / newline |
| `Shift+Tab` | Cycle permission mode |
| `Ctrl+P` | Command palette |
| `Ctrl+O` | Conversation switcher |
| `Ctrl+N` | New conversation |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+1..9` | Focus conversation tab / agent pane (context-dependent: tabs when no panes open) |
| `Ctrl+F` | Zoom focused pane |
| `F1` | Help overlay |
| `F2` | Model picker |
| `PgUp/PgDn`, wheel | Scroll transcript |

## Theming

TCSS theme file; dark default matching QuickTerm's aesthetic. All colors route through theme variables so a light theme is config, not code. Minimum terminal size 100×28 with a graceful "window too small" screen below that.
