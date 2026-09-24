# Contributing

QuickCode uses Python 3.12+ and [uv](https://docs.astral.sh/uv/). The
frontend is plain JavaScript and CSS served straight from
`quickcode/frontend/` — no Node build step.

```powershell
uv sync --all-extras --dev
uv run --no-sync pytest -q
uv run --no-sync ruff check quickcode tests scripts
node --test tests/js/*.test.mjs        # when you changed frontend JavaScript
```

`uv run --no-sync python scripts/release.py --check` runs all of that plus a
byte-compile, `node --check` over every frontend module and `git diff --check`
— the local release gate. CI (`.github/workflows/ci.yml`) runs ruff, pytest and
the frontend syntax check on Windows with Python 3.12, 3.13 and 3.14 for every
push and pull request. The suite also runs on Linux and macOS; see `AGENTS.md`
for the handful of Windows-shaped tests that do not pass there.

## Ground rules

- Tools, providers, and MCP servers are plugins — see `AGENTS.md` for the
  shapes those extension points expect. The built-in web UI is not a
  plugin surface; keep it coherent rather than configurable.
- A tool that mutates anything must declare a `permission: PermissionSpec`
  (`quickcode/core/permissions.py`). The engine no longer recognizes tools
  by name, so an undeclared tool is treated as mutating and prompted for —
  which is safe, but wrong for a read-only tool, so declare `READ_LIKE`
  explicitly.
- Session/event-log format changes are `locked`-tier by convention: the
  trajectory view and resume/replay both depend on it. Widen the format
  additively; don't change the meaning of an existing field.
- Match the existing code's style before introducing a new one — the
  codebase favors small, focused modules (see `quickcode/kernel/`,
  `quickcode/tools/`) over growing an existing file into a monolith.
- Keep command output as raw bytes on the PTY hot path
  (`quickcode/pty/session.py`) — decode once, at the boundary
  (`tools/base.py::decode_output`).

## Tests

pytest with `asyncio_mode = auto` (see `pyproject.toml`); tests live in
`tests/`. Prefer testing through the FastAPI `TestClient` and the real
permission engine over mocking deep internals — most existing tests do this
and it catches wiring bugs that a mock would hide.

## Before large changes

Open an issue or discuss first for anything that touches the plugin kernel,
the permission engine's decision order, or the session event-log schema —
these are the contracts other code (and the trajectory UI) depends on.
