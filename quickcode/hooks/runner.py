"""Running one hook command: a shell, a JSON payload on stdin, a deadline.

Everything that makes a child process behave here is borrowed rather than
restated: the shell the ``bash`` tool uses (Git Bash, else PowerShell, on
Windows; ``/bin/bash`` elsewhere), ``subproc`` for the spawn itself -- no
console window, none of the app's credentials, a process group of its own so
the deadline's kill takes the hook's children with it -- and ``decode_output``
for a child that does not speak UTF-8.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode import subproc
from quickcode.tools.base import ToolCtx, decode_output
from quickcode.tools.bash import _build_argv

# Read at most this much of either stream into the transcript's orbit.
OUTPUT_CAP_BYTES = 256 * 1024

# How long to wait for the pipes to drain once the tree has been killed.
_DRAIN_S = 5.0

PROJECT_DIR_ENV = "QUICKCODE_PROJECT_DIR"


@dataclass(frozen=True)
class Completed:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    ms: int = 0
    timed_out: bool = False
    spawn_error: str = ""


def hook_env(cwd: Path) -> dict[str, str]:
    """The environment a hook runs in: every child's, plus the project root.

    A hook is a program the user (or a trusted project) chose to run, so it
    gets the environment a terminal would give it -- which is QuickCode's
    minus its own API keys (``subproc.child_env``): no hook needs them, and a
    hook that logs its environment should not be how they leak.
    """
    env = subproc.child_env({PROJECT_DIR_ENV: str(cwd)})
    # The payload is UTF-8. A Python hook on Windows would otherwise read its
    # stdin in the ANSI code page and mangle every non-ASCII path it is sent.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _text(raw: bytes) -> str:
    return decode_output(raw[:OUTPUT_CAP_BYTES]).replace("\r\n", "\n")


async def run_command(
    command: str, *, payload: dict[str, Any], ctx: ToolCtx, timeout_s: float,
) -> Completed:
    """Run ``command`` with ``payload`` as JSON on stdin, in the project root."""
    argv = _build_argv(command, ctx)
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    cwd = Path(ctx.cwd)
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        proc = await subproc.spawn_async(argv, cwd=str(cwd), env=hook_env(cwd),
                                         stdin=subproc.PIPE)
    except OSError as exc:
        return Completed(None, ms=elapsed(), spawn_error=str(exc))
    # An interrupted turn cancels this; `communicate` kills the tree first.
    out, err, timed_out = await subproc.communicate(proc, data, timeout=timeout_s,
                                                    drain_s=_DRAIN_S)
    code = None if timed_out else proc.returncode
    return Completed(code, _text(out), _text(err), elapsed(), timed_out)
