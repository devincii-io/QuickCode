# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/).

## [Unreleased] — 2.1.0

### Added

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

[Unreleased]: https://github.com/devincii-io/QuickCode/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/devincii-io/QuickCode/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/devincii-io/QuickCode/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/devincii-io/QuickCode/releases/tag/v1.0.0
