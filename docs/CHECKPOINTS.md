# Checkpoints and rewind

Before the first change a turn makes to a file, QuickCode saves what the file
held. A **rewind** puts files back to how they were before a chosen turn: all
of them, or the ones picked. It is the undo for the agent's file edits, and it
is a user action, not a tool the model can call.

This document describes what is recorded, the rewind, its HTTP API and the
part of the app that drives it (§In the app).

The code is `quickcode/checkpoints/` (one module per concern: store, snapshot,
recorder, hook, rewind, diff, paths, events) and `quickcode/server/checkpoints_api.py`;
the UI is `js/chat/rewind.js`, `js/checkpoints/` and `js/panels/checkpoints.js`.

## What is recorded

Checkpoints are taken by a loop hook (`quickcode/checkpoints/hook.py`), not by
the tools. Its `check_tool` seam runs after the permission gate and before the
call; its `tool_feedback` seam runs after the call. Between them it brackets
every call to a tool whose `PermissionSpec` says it writes files: mutating,
not a shell, and a path target (or `permission_paths`, for a tool with
several). No tool knows it is being checkpointed, and nothing names a tool.

| A change made by | Checkpointed | Why |
|---|---|---|
| `write`, `edit` | yes | they declare `file_path` as a path target |
| an authored command tool | the files its `path` parameters name | `permission_paths` lists them |
| a plugin tool declaring `path_target` | yes | same declaration the gate reads |
| `bash`, a background shell job, anything a command starts | **no** | a command line names no files anyone can check |
| an MCP tool | **no** | it declares no path target |
| a file outside the project | **no** | only the project is QuickCode's to put back |
| a directory | **no** | only regular files are saved |

**Files changed by `bash` cannot be tracked this way**, and every listing and
preview says so in its `untracked` field. What a rewind *can* do about them is
notice: a tracked file that `bash` changed after the agent's last recorded edit
no longer matches what that edit left, and the rewind reports it as a conflict
instead of silently overwriting it.

Rules the recording follows:

- **Once per file per turn.** The first change a turn makes to a file saves the
  file's previous bytes (or the fact that it did not exist). Later changes in
  the same turn only move the recorded end state along and add their call id,
  so three edits in one turn still rewind to the bytes from before the first.
- **Turns are the session log's turns.** The number is the one
  `TranscriptRecorder` stamps on the turn's `user_message`, so "before turn 4"
  is the turn the transcript shows as 4. A reopened conversation reads its
  number off the log and counts on.
- **A subagent's edits land in the turn that is running.** The hook is carried
  into every child (`hooks/plugin.py::child_hooks`) sharing the conversation's
  recorder; a delegation is not a user turn.
- **Only real changes.** A file whose bytes are the same after the call as
  before it is not recorded. A tool that does not run a program is taken at its
  word that an error means it wrote nothing; one that runs a program
  (`executes`, an authored command tool) may write and then fail, so its
  changes are recorded either way.
- **The before-half is read at the gate.** In `ask` mode a permission prompt
  sits between the read and the write. `write` and `edit` refuse a file that
  changed since the model read it, so for them the gap cannot record a change
  that did not happen. For a command tool, a file changed by hand during the
  prompt is part of "before the turn".
- **Symlinks are resolved.** A file is recorded at its real path relative to
  the project root, so an edit through a link inside the project is recorded
  (and rewound) at the file it points to.

## Where it is stored

```
<project>/.quickcode/checkpoints/
  .gitignore               "*" -- these are copies of project files, secrets included
  <conv_id>/
    index.json             which files each turn changed, and what they held before
    blobs/<sha256>         the bytes, content-addressed
```

Blobs are named by the SHA-256 of their contents, so a turn that starts from
the same bytes as an earlier one stores nothing new, and a blob is checked
against its name before it is restored. `index.json` is small and rewritten
whole through `quickcode/fsutil.py`, under a lock the recording hook and the
API share. An index that cannot be parsed is moved aside as
`index.damaged-<time>.json`, never overwritten in place.

The directory is removed with its session (`purge_sessions`), and ignored by
git twice over: `checkpoints/` is in the `.quickcode/.gitignore` new projects
get, and the directory writes its own `.gitignore` because a project created
before checkpoints existed has a `.quickcode/.gitignore` QuickCode never
rewrites.

### Limits, and saying so when they bite

| Limit | Value | When it is exceeded |
|---|---|---|
| Per conversation | 128 MiB of blobs | the oldest turns' snapshots are evicted, oldest first, until the new one fits |
| Per file | 8 MiB | that file's snapshot is not kept |

A snapshot that could not be kept is not silently missing: its entry stays in
the index with `restorable: false` and a reason, and a rewind that needs it
skips that file and says why. A file the turn *created* needs no snapshot to
undo, so eviction never takes that away.

| `reason` | Meaning |
|---|---|
| `too_large` | over the per-file limit when the turn first changed it |
| `size_cap` | the turn alone did not fit under the conversation's limit |
| `evicted` | saved, then dropped to make room for newer turns |
| `damaged` | the saved copy is missing, or no longer matches its digest |

## Rewinding

For one file, "before turn N" is what the earliest turn at or after N that
changed it saved -- or its absence, if that turn created it. What the file
should hold *now* is what the latest such turn left. Each file in a rewind
gets one action:

| `action` (preview) | Done as | What happens on disk |
|---|---|---|
| `restore` | `restored` | the saved bytes are written back |
| `delete` | `deleted` | the file did not exist before the turn; it is removed |
| `create` | `created` | the file existed before the turn and is gone now; it is written back |
| `none` | `unchanged` | it already holds the saved bytes |

Two checks run before anything is written, and both are **conflicts**: the
rewind is refused (409) unless `force` is set.

| Conflict `kind` | Means |
|---|---|
| `modified` | the file on disk is not what the last recorded change left: `bash`, another program or a person wrote it since |
| `intervening` | between two recorded turns the file changed in a way no recorded change explains, so a rewind would discard that too |

Some files are never written, forced or not: one whose saved copy is not
restorable, one whose path now leads outside the project or through a symlink
or junction created since the checkpoint (a rewind writes where it recorded, or
nowhere), and one where something other than a regular file now stands. They
are listed in `skipped` with the reason.

How the bytes go back:

- **Exactly.** The saved bytes are the bytes: encoding, byte-order mark, line
  endings and binary content come back as they were, with no decoding step in
  between.
- **Atomically**, through `quickcode/fsutil.py`, keeping the permission bits of
  the file being replaced. A file written back into a directory that is gone
  gets the directory recreated; a directory a turn created is left in place
  when the rewind deletes the files in it.
- **Keeping what it replaces.** Before a file is overwritten or removed, its
  current bytes are saved as a blob if they fit in the space left (a rewind
  never evicts to make room for its own backups). Each file in the result says
  `backup: true|false`. There is no route to restore a backup yet; see
  follow-ups.
- **Once.** The entries a rewind undid are marked with its id and take no part
  in later rewinds, so rewinding to before turn 5 and later to before turn 3
  chains correctly instead of reporting the first rewind as a conflict.
- **Refused while the conversation works:** a running turn, a waiting
  permission prompt or a background subagent job could be writing the very
  files being restored (409).
- **The model is told.** The next turn starts with a reminder naming the files
  that were put back and asking it to re-read them before editing. Its history
  is not rewound: it still remembers making those edits.

A rewind is not a tool call and goes through no permission rule: the person
pressing the button is the one the gate would ask. The routes sit behind the
same loopback token as the rest of the API (docs/ARCHITECTURE.md §Trust boundary).

## HTTP API

Each route is mounted under `/api` (the default project) and
`/api/projects/{pid}` (any open project).

| Method | Path | Body | Answers |
|---|---|---|---|
| `GET` | `/sessions/{conv_id}/checkpoints` | -- | every turn that changed files |
| `POST` | `/sessions/{conv_id}/checkpoints/preview` | `{turn, paths?}` | what a rewind would do, with diffs and conflicts |
| `POST` | `/sessions/{conv_id}/checkpoints/rewind` | `{turn, paths?, force?}` | the rewind's result |

`turn` is a turn number (1 or more); `paths`, when given, is a non-empty list
of at most 1000 paths exactly as the listing spells them, and naming a file
that has no checkpoint at or after `turn` is a 400 listing them. Errors: 404
for an unknown conversation, 400 for a malformed body, 409 for a conflict or a
busy conversation.

Listing (`GET …/checkpoints`):

```json
{
  "conv_id": "3f9c1a2b4d5e",
  "checkpoints": [
    {
      "turn": 3,
      "time": "2026-09-24T10:14:03",
      "restorable": true,
      "files": [
        {"path": "src/app.py", "change": "modified", "added": 4, "removed": 1,
         "binary": false, "restorable": true, "reason": "", "rewound": null,
         "tool": "edit", "agent": "main", "calls": ["toolu_01", "toolu_02"],
         "time": "2026-09-24T10:14:03"}
      ]
    }
  ],
  "rewinds": [
    {"id": "rw4", "time": "2026-09-24T10:20:11", "turn": 3, "forced": false,
     "files": [{"path": "src/app.py", "action": "restored", "from_turn": 3, "backup": true}]}
  ],
  "storage": {"used_bytes": 18233, "max_bytes": 134217728, "max_file_bytes": 8388608},
  "untracked": "Only changes made by write, edit and other tools that declare the file they write are checkpointed. …"
}
```

`change` is `created`, `modified` or `deleted` for what that turn did to the
file; `added`/`removed` count lines from before the turn to where it left the
file, and are `null` for a file too large to compare or whose saved copy is
gone. `rewound` is the id of the rewind that undid the entry.

Preview (`POST …/checkpoints/preview`), and the 409 a conflicting rewind
answers with in `detail.conflicts`, describe each file as:

```json
{
  "turn": 3,
  "files": [
    {"path": "src/app.py", "from_turn": 3, "last_turn": 5, "action": "restore",
     "restorable": true, "reason": "", "blocked": "",
     "conflicts": [{"kind": "modified", "detail": "changed on disk since turn 5's recorded edit -- …"}],
     "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n…",
     "added": 1, "removed": 1, "binary": false, "truncated": false, "omitted": ""}
  ],
  "conflicts": ["src/app.py"],
  "not_restorable": [],
  "untracked": "…"
}
```

The diff runs from what is on disk now to what the rewind would write, with
line endings kept on each line. It is omitted (`omitted: "binary"` or
`"too_large"`, over 2 MB a side) rather than faked, and cut at 100 000
characters with `truncated: true`.

Rewind (`POST …/checkpoints/rewind`):

```json
{
  "turn": 3,
  "rewind_id": "rw4",
  "files": [{"path": "src/app.py", "action": "restored", "from_turn": 3, "backup": true}],
  "skipped": [{"path": "assets/big.bin", "reason": "too_large"}],
  "conflicts": [],
  "not_restorable": [{"path": "assets/big.bin", "reason": "too_large"}],
  "untracked": "…"
}
```

`rewind_id` is `null` when nothing was put back.

## The session log

Two records, both new types (the log's schema widens only by addition), both
carrying paths and never file contents:

| Type | When | Fields |
|---|---|---|
| `checkpoint` | a turn saved a file before its first change | `turn`, `path`, `call_id`, `tool`, `created` (it did not exist), `restorable`, `reason` |
| `files_rewound` | a rewind put files back | `rewind_id`, `to_turn`, `files` (`[{path, action, from_turn}]`, at most 200), `file_count`, `skipped`, `forced` |

A subagent's `checkpoint` is logged inside its `agent_event` wrapper, like the
rest of its activity. A rewind of an open conversation goes through it, so
attached windows receive `files_rewound` live; one of a conversation nobody has
open is appended to its log directly.

## In the app

**In the transcript**, a user message whose turn changed files gets a small
**↺ Rewind files** button under it (a real button, so `Tab` reaches it; its
accessible name is "Rewind files to before turn N"). It appears when the
turn's first `checkpoint` record arrives, live or in a replay, and goes once a
rewind has undone every file that turn changed. The transcript finds the turn
by its number, the way tool results find their card (`js/chat/rewind.js`,
beside `chat/registry.js`), never by searching the page. A `files_rewound`
record leaves a one-line note where it happened, which expands to the files
and what was done to each.

**The dialog** (`js/checkpoints/dialog.js`) opens on the preview and lists
every file the rewind would touch:

- what it would do (*restore*, *delete*, *re-create*, *already as before*),
  the `+`/`−` line counts, and the unified diff behind a disclosure — `−` is
  what is on disk now, `+` what the rewind writes. Binary files and files too
  large to compare say so instead of showing a diff.
- a checkbox per file. A file with a **conflict** is drawn in red with the
  server's explanation, and starts unselected: rewinding it discards a change
  nobody recorded. **Overwrite anyway** takes the conflicted files in and is
  the API's `force`; it is only sent when a conflicted file is actually picked.
- a file that cannot be put back (its copy was not kept, or its path now leads
  through a link) is listed, disabled, with the reason.
- the `untracked` notice, always: **bash changes are not tracked**.

The rewind names the picked paths, so it does what was previewed and nothing
a later turn added. While a turn runs or a permission prompt waits, Rewind is
disabled with the same sentence the API would answer with; a background job
the pane cannot see is refused by the server, and its words are shown as
given. A conflict that appears between the preview and the click (the 409's
`detail.conflicts`) marks those files and leaves them out rather than
overwriting them. Afterwards the dialog shows what was restored, deleted,
re-created or left alone and why. The Files panel refreshes on `files_rewound`.

**The Checkpoints tab** of the side panel (`js/panels/checkpoints.js`) lists
the listing route's answer, newest turn first: each file with what the turn
did to it, its line counts, the subagent that changed it, and whether a rewind
already undid it or its copy was not kept; the storage the checkpoints use;
and a **Rewind…** button per turn that opens the same dialog. It is the way
back to a turn whose message is far up the transcript, and it refreshes on
the two records.

**In the Trajectory**, both records read as a sentence: `checkpoint
src/app.py · turn 3 · saved before its first change · edit`, and `files
rewound to before turn 3 · 2 files: src/app.py restored, notes.md deleted`.

What the dialog may send is decided in `js/checkpoints/model.js`, which has no
DOM and is covered by `tests/js/checkpoints.test.mjs`. `scripts/smoke_rewind.js`
drives the whole path in Chromium against `scripts/workspace_smoke_server.py
<port> --edits`, whose preview model edits `README.md` and writes `notes.md` in
each conversation's first turn, in auto-edit mode, and checks the files on disk.

## Why not a git snapshot for bash

`git stash create` per turn was considered as a best-effort net for what `bash`
changes, and left out: it scans the whole working tree at the start of every
turn, writes objects into the user's repository, needs the index lock the
user's own git commands also want, and does not see untracked files -- which
are most of what a build or a generator creates. Cheap and robust it is not.

## Follow-ups

- **Conversation rewind.** Truncating the history to before turn N, so the
  model forgets the edits a file rewind undid. Out of scope here; today the
  model is told instead.
- **Undo a rewind** from the backups each rewind keeps.
- **The limits as settings** rather than constants in
  `quickcode/checkpoints/store.py`.
