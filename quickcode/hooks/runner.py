"""Running one hook command: a shell, a JSON payload on stdin, a deadline.

Everything that makes a child process behave here is borrowed rather than
restated: the shell the ``bash`` tool uses (Git Bash, else PowerShell, on
Windows; ``/bin/bash`` elsewhere), ``subproc`` so no console window flashes up
behind the windowed app, ``decode_output`` for a child that does not speak
UTF-8, and the PTY module's process-tree kill for the deadline.

A hook gets its own process group (POSIX) so the kill takes its children with
it, and only them -- ``killpg`` on a hook that shared the server's group would
take down the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode import subproc
from quickcode.pty.session import _kill_tree
from quickcode.secrets import API_KEY_ENV
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
    """The environment a hook runs in: the app's own, minus its model key.

    A hook is a program the user (or a trusted project) chose to run, so it
    gets the environment a terminal would give it. QuickCode's own API key is
    the one thing taken out: no hook needs it, and a hook that logs its
    environment should not be how it leaks.
    """
    env = {k: v for k, v in os.environ.items() if k != API_KEY_ENV}
    env[PROJECT_DIR_ENV] = str(cwd)
    # The payload is UTF-8. A Python hook on Windows would otherwise read its
    # stdin in the ANSI code page and mangle every non-ASCII path it is sent.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run_blocking(
    argv: list[str], cwd: str, env: dict[str, str], data: bytes, timeout_s: float,
    holder: list,
) -> tuple[int | None, bytes, bytes, bool]:
    proc = subproc.popen(
        argv, cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=not subproc.IS_WINDOWS,
    )
    holder.append(proc)
    try:
        out, err = proc.communicate(data, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=_DRAIN_S)
        except Exception:  # noqa: BLE001 - a wedged pipe is not worth a crash
            out, err = b"", b""
        return None, out or b"", err or b"", True
    return proc.returncode, out or b"", err or b"", False


def _text(raw: bytes) -> str:
    return decode_output(raw[:OUTPUT_CAP_BYTES]).replace("\r\n", "\n")


async def run_command(
    command: str, *, payload: dict[str, Any], ctx: ToolCtx, timeout_s: float,
) -> Completed:
    """Run ``command`` with ``payload`` as JSON on stdin, in the project root."""
    argv = _build_argv(command, ctx)
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    cwd = Path(ctx.cwd)
    holder: list = []
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        code, out, err, timed_out = await asyncio.to_thread(
            _run_blocking, argv, str(cwd), hook_env(cwd), data, timeout_s, holder
        )
    except asyncio.CancelledError:
        # The turn was interrupted. Cancelling this coroutine does not reach
        # the child, which the worker thread is still waiting on.
        proc = holder[0] if holder else None
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
        raise
    except (OSError, ValueError) as exc:
        # ValueError is Popen refusing the argv itself -- a NUL in the command,
        # which a settings file can spell as \u0000. Still a hook that could
        # not start, so it fails open like one.
        return Completed(None, ms=elapsed(), spawn_error=str(exc))
    return Completed(code, _text(out), _text(err), elapsed(), timed_out)
