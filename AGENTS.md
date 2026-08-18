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
uv run --no-sync pytest -q                           # tests (~10 s)
uv run --no-sync ruff check quickcode tests scripts
.venv\Scripts\python.exe scripts\release.py --check  # tests + ruff, the local release gate
```

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
  byte range each contributed, which is what lets the UI attribute a run of
  prompt text to a specific section. Two invariants any change here must
  keep: **byte-stability within a session** (the prompt-cache breakpoint
  sits on the system message — same inputs, same bytes) and **the tool-use
  policy section stays `locked`** (it's the contract the loop and the
  trajectory view depend on, not a matter of prompt-tuning taste).
- **Permission engine** (`quickcode/core/permissions.py`) — modes `plan →
  ask → auto-edit → dontask → yolo`; evaluation order is deny → ask → allow
  → mode default; bash commands are decomposed per subcommand, never
  prefix-matched; protected paths (`.git`, `.quickcode`, `.env*`, `.ssh`,
  anything outside the project root) always prompt before any allow rule
  applies; circuit breakers (`rm -rf /`, forced push, fork bombs) prompt
  even in `yolo`. Full rule syntax and scope precedence: `docs/PERMISSIONS.md`.
- **Session event log** (`quickcode/session/store.py`) — append-only JSONL;
  the system prompt, every tool call/result, subagent activity, and
  permission decisions all land here. The **Trajectory** view, resume, and
  replay all derive from this single stream — treat its schema as `locked`:
  widen additively, never repurpose an existing field.
- **Native app window** (`quickcode/ui/window.py`) — thin wrapper around
  `pywebview.create_window`/`.start()`; `available()` gates on pywebview
  being importable, and `quickcode/webapp.py` falls back to the system
  browser when it isn't. Must be started on the main thread — the server
  runs in a background thread instead when the window is used.
- **PTY** (`quickcode/pty/session.py`) — `pywinpty` (ConPTY) on Windows, the
  POSIX `pty` module elsewhere; patterns carried over from QuickTerm (bytes
  in/bytes out on the hot path, no decoding).

## Conventions

- Server handlers that need to be stubbable in tests import via
  `importlib.import_module("quickcode.X")`, same convention as QuickTerm —
  don't switch these to a plain `import` without checking why they were
  importlib-loaded in the first place.
- Ruff config: `line-length = 100`, target `py312`, `select = ["E", "F",
  "I", "UP", "B"]`. `E501` (line length) is deliberately ignored — don't
  fight the formatter over wrapping; `UP042/046/047` are ignored because
  QuickCode's `Generic[In]` / `Enum` subclasses predate the newer syntax
  ruff would otherwise suggest.
- Tests: pytest, `asyncio_mode = auto`. Prefer exercising the real
  `PermissionEngine` and FastAPI `TestClient` over deep mocking — most of
  the existing suite does this and it catches wiring bugs a mock would
  hide. Keep the suite fast (currently ~10 s for 139 tests).
- No secrets in the session event log or diagnostics — API keys live in
  `secrets.py`-managed storage, never in a tool call's recorded arguments
  if the tool can avoid it.
- The frontend has no build step: plain ES modules under
  `quickcode/frontend/js/`, served with cache headers that make sense for
  a local app (check `server/app.py` before assuming browser caching is
  the same story as QuickTerm's `no-cache` requirement — verify rather than
  copy that detail across).

## Local release workflow

QuickCode does not freeze into a single binary the way QuickTerm does
(no PyInstaller spec, no `.spec` file) — the Windows installer
(`packaging/quickcode.iss`) provisions Git/Python, creates a private venv,
and `pip install`s the built wheel into it at install time. The version that
matters at runtime is `pyproject.toml`'s `[project].version`, read back via
`importlib.metadata.version("quickcode")` in `quickcode/cli.py`; the
hardcoded `__version__` in `quickcode/__init__.py` is unused dead code, not
a second source of truth to keep in sync (do not treat it like QuickTerm's
`__init__.py`/pyproject/`uv.lock` three-way invariant — it isn't one).

```powershell
.venv\Scripts\python.exe scripts\release.py --version 2.0.0   # bump pyproject.toml, uv lock, build, checksum
```

See `scripts/release.py` for exactly what that runs; it mirrors QuickTerm's
`scripts/check.py` + manual release steps, adapted for an installer that
pip-installs rather than a frozen `dist/` folder.

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
