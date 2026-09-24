# QuickCode — agent guide

Local-first coding agent: FastAPI + uvicorn backend on 127.0.0.1, a vanilla
ES-module frontend with no Node build step (`quickcode/frontend/`), opened
in a native pywebview window (`quickcode/ui/window.py`) or the default
browser as fallback. One backend process hosts many projects
(`server/projects.py::ProjectHub`), like editor windows; the frontend
attaches to it over REST + WebSocket. Models are reached through a
provider-agnostic layer (`providers/openai_compat.py`, OpenRouter by
default, any OpenAI-compatible endpoint by config).

This is a separate document from `docs/AGENTS.md`, which covers subagent
orchestration, teammate mode, and the task board as a product feature. This
file is for anyone (human or agent) editing the repo itself.

## Commands

Everything runs through `uv`. Never run `uv sync/add/lock` to "fix" the test
env unless you actually changed `pyproject.toml`.

```powershell
uv sync --all-extras --dev                          # once, or after pyproject.toml changes
uv run --no-sync quickcode                           # run the app (native window; qc also works)
uv run --no-sync pytest -q                           # tests (~2,650, ~2 min)
uv run --no-sync ruff check quickcode tests scripts
node --test tests/js/*.test.mjs                      # frontend unit tests
.venv\Scripts\python.exe scripts\release.py --check  # the local release gate: tests, ruff,
                                                     # byte-compile, JS checks, git diff --check
```

The commands are the same on Linux and macOS (`python scripts/release.py`
instead of the `.venv\Scripts` path). The suite runs there too; CI
(`.github/workflows/ci.yml`) covers Windows only, and a handful of tests are
known to fail off it (symlink and junction handling, `WindowsPath`, the
installer layout).

## Architecture (the load-bearing pieces)

- **Plugin kernel** (`quickcode/kernel/`) — `spec.py` defines `PluginSpec` /
  `SettingSpec` with a three-tier mutability model (`free`, `confirm`,
  `locked`); `registry.py` holds the live set; `bootstrap.py` assembles it
  from the real tool registry, provider factories, subagent definitions, and
  MCP configs so Settings shows the install the runtime actually has.
  Everything QuickCode ships is an "internal" plugin — same shape as a
  third-party one, no privileged side door.
- **Tools** (`quickcode/tools/`) — subclass `Tool` (`tools/base.py`), declare
  `name`, `Input` (a Pydantic model → strict JSON Schema), `is_read_only`,
  and **`permission: PermissionSpec`**. The permission engine
  (`core/permissions.py`) reads that spec off the tool instead of matching
  on its name — `mutates`, `target_field` (which input field a rule
  matches against), `path_target` / `shell` (how the target is
  interpreted). An undeclared tool defaults to `DEFAULT_SPEC` (mutating,
  prompted) — the safe default, but wrong for a read-only tool, so declare
  `READ_LIKE` explicitly rather than relying on it.
- **System prompt** (`quickcode/prompts/system.py` + `prompts/sections.py`)
  — composed from an ordered list of `PromptSection`s rather than one
  conditional template. Each section has an id, an order, a mutability
  tier, and a renderer; `compose()` joins the non-empty ones and reports the
  range each contributed (character offsets into the `str`, not bytes —
  docs/PROMPTS.md §1), which is what lets the UI attribute a run of prompt
  text to a specific section. Two invariants any change here must
  keep: **byte-stability within a session** (the prompt-cache breakpoint
  sits on the system message — same inputs, same bytes) and **the tool-use
  policy section stays `locked`** (it's the contract the loop and the
  trajectory view depend on, not a matter of prompt-tuning taste).
- **Permission engine** (`quickcode/core/permissions.py`) — modes `plan →
  ask → auto-edit → dontask → yolo`; evaluation order is deny → ask → allow
  → mode default; bash commands are decomposed per subcommand, never
  prefix-matched; protected paths (`.git`, `.quickcode`, `.env*`, `.ssh`,
  anything outside the project root) prompt before any allow rule applies,
  in every mode but `yolo` (`dontask` denies instead); circuit breakers
  (`rm -rf /`, forced push, fork bombs) prompt even in `yolo`. Full rule
  syntax and scope precedence: `docs/PERMISSIONS.md`.
- **Session event log** (`quickcode/session/store.py`) — append-only JSONL;
  the system prompt, every tool call/result, subagent activity, and
  permission decisions all land here. The **Trajectory** view, resume, and
  replay all derive from this single stream — treat its schema as `locked`:
  widen additively, never repurpose an existing field.
- **Native app window** (`quickcode/ui/window.py`) — thin wrapper around
  `pywebview.create_window`/`.start()`; `available()` gates on pywebview
  being importable (and, on Windows, the WebView2 runtime being
  present), and `quickcode/webapp.py` falls back to the system
  browser when it isn't. Must be started on the main thread — the server
  runs in a background thread instead when the window is used.
- **Session assembly** (`quickcode/session/assemble.py`) — `build_session`
  is the only place a session is put together, for the app
  (`ConversationManager.open`) and for headless `-p` alike: pool, preset,
  composition, limits, mode, permissions, prompt, hooks. Don't build an
  `AgentInstance` for a session anywhere else.
- **Child processes** (`quickcode/subproc.py`) — every process QuickCode
  starts goes through `spawn`/`spawn_async`/`run`: no console window,
  `child_env()` (QuickCode's API keys removed), its own process group, and
  `kill_tree`. `tests/test_no_console_window.py` fails on a spawn anywhere
  else. Git goes through `quickcode/gitcmd.py`, which also switches off the
  repository's hooks, fsmonitor, textconv and filter drivers.
- **Settings files** (`quickcode/kernel/settings_file.py`, `jsonfile.py`,
  `textio.py`) — one BOM-aware reader for every settings/config JSON and
  every hand-edited text file, and one writer; a project write goes through
  `write_project_settings`, which keeps the project's trust.
- **PTY** (`quickcode/pty/`) — `session.py` runs one `bash` command per
  pseudo-terminal on POSIX; on Windows `bash` uses plain pipes by default,
  because under a tty a command that reads stdin waits for nobody.
  `interactive.py` is the terminal panel's long-lived shell (ConPTY via
  `pywinpty` on Windows). Patterns carried over from QuickTerm: bytes on the
  hot path, decoded once at the boundary (`tools/base.py::decode_output`),
  never with `surrogateescape`.

## Conventions

- The frontend entry is `js/entry.js`. The outer `workspaces.js` owns the
  workspace sidebar, split tree, and saved layout. Each `?pane=1` iframe runs
  `main.js` with its own store, socket, composer, and reviews. Never reparent
  mounted iframes: moving their DOM nodes reloads them. Layout changes update
  absolute boxes using `split_tree.js`, adapted from QuickTerm. Message bridges
  validate both origin and the exact window source. Tokens remain in tab
  sessionStorage; workspace localStorage holds only names and session/layout
  identifiers. Empty conversations are not restored by ID after server restart.
- `tests/js/*.test.mjs` runs through Node's built-in test runner and is included
  in `scripts/release.py --check`. `scripts/workspace_smoke_server.py` provides
  a disposable project/provider environment for the browser workflow in
  `scripts/smoke_workspaces.js`. It must never use the user's real config or API.
- Appearance preferences are shared through `js/appearance.js`. Theme save
  broadcasts use storage events so existing panes update without reloading.

- Server handlers that need to be stubbable in tests import via
  `importlib.import_module("quickcode.X")`, same convention as QuickTerm —
  don't switch these to a plain `import` without checking why they were
  importlib-loaded in the first place. (As of 2.7.0 no handler needs it —
  the suite stubs with `monkeypatch.setattr("quickcode.x.y", ...)` — so this
  is the pattern to reach for when one does.)
- Ruff config: `line-length = 100`, target `py312`, `select = ["E", "F",
  "I", "UP", "B"]`. `E501` (line length) is deliberately ignored — don't
  fight the formatter over wrapping; `UP042/046/047` are ignored because
  QuickCode's `Generic[In]` / `Enum` subclasses predate the newer syntax
  ruff would otherwise suggest.
- Tests: pytest, `asyncio_mode = auto`. Prefer exercising the real
  `PermissionEngine` and FastAPI `TestClient` over deep mocking — most of
  the existing suite does this and it catches wiring bugs a mock would
  hide. Keep the suite fast (currently ~2 min for about 2,650 tests on
  Linux).
- No secrets in the session event log or diagnostics — API keys live in
  `secrets.py`-managed storage, never in a tool call's recorded arguments
  if the tool can avoid it.
- The frontend has no build step: plain ES modules under
  `quickcode/frontend/js/`, served with cache headers that make sense for
  a local app (check `server/app.py` before assuming browser caching is
  the same story as QuickTerm's `no-cache` requirement — verify rather than
  copy that detail across).

## Local release workflow

QuickCode freezes into a PyInstaller **onedir** folder (`quickcode.spec` at
the repo root), the way QuickTerm does, with one difference: it produces two
executables out of one `dist/QuickCode` — `quickcode.exe` (console CLI) and
`QuickCodeApp.exe` (windowed app). The windowed one cannot be called
`QuickCode.exe`; Windows file names are case-insensitive, so PyInstaller
would build both and silently overwrite the first with the second. The
installer (`packaging/quickcode.iss`) now only *copies* that folder — no
Git, no Python, no venv, no network at install time. Wheel and sdist are
still built, so `pip install` stays supported.

The version that matters at runtime is `pyproject.toml`'s
`[project].version`, read back via `importlib.metadata.version("quickcode")`
(`quickcode/__init__.py` resolves `__version__` lazily from the same place —
it holds no literal, so there is no QuickTerm-style three-way invariant to
keep in sync). The frozen build carries that dist-info via `copy_metadata`,
which means **the build environment must match pyproject.toml**: freeze
against a stale editable install and `--version`, `/api/health` and the
update check all report yesterday's number. `scripts/release.py` refuses to
freeze when the two disagree, and `uv sync`s after a version bump.

```powershell
.venv\Scripts\python.exe scripts\release.py --version 2.0.0   # bump pyproject.toml, uv lock+sync, check, build, checksum
```

See `scripts/release.py` for exactly what that runs — `uv build`, then
PyInstaller against `quickcode.spec`, then ISCC around its output, then
SHA256SUMS.txt over all three artifacts. `packaging/README.md` documents the
installer itself.

## Security model

Server binds 127.0.0.1 only. The permission engine (`core/permissions.py`)
is the safety boundary, not the model: every mutating tool call is gated by
mode + rules + protected-path checks before it runs. Keep new tools honest
about their `permission` shape — a plugin tool that writes files and
declares `READ_LIKE` (or omits `permission` and relies on a stale default)
is a real security bug, not a style nit.

## Author / license

MIT. Author: Fichtel Systems (Devin Isaac Worbis). `pyproject.toml`,
`LICENSE`, and `packaging/quickcode.iss` must stay consistent on publisher
name.
