"""An MCP server as an operating-system process: start it, drain it, end it.

The JSON-RPC session lives in ``mcp.py``; this is everything underneath it that
is about pipes and process trees rather than protocol.

* **Line limit.** asyncio's ``StreamReader`` refuses a line over 64 KiB by
  default, and a stdio MCP message is one line: a large ``tools/list`` or tool
  result killed the reader and every call after it hung until its timeout.
* **stderr is drained**, into a bounded tail. Left as a pipe nobody reads it
  would fill and block the server mid-write; sent to ``DEVNULL`` it threw away
  the one thing that says why a server would not start.
* **Shutdown is the spec's**: close stdin, wait, then terminate, then kill --
  and the *tree*. ``npx some-server`` is npx, then node; terminating the direct
  child left the server itself running. On POSIX the server leads its own
  process group; on Windows ``taskkill /T`` walks the tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from quickcode import subproc
from quickcode.security import launch

MAX_LINE_BYTES = 64 * 1024 * 1024
STDERR_TAIL_BYTES = 8 * 1024
GRACE_S = 2.0


async def spawn(
    command: str, args: list[str], env: dict[str, str], cwd: Path | None,
) -> asyncio.subprocess.Process:
    kwargs = {} if subproc.IS_WINDOWS else {"start_new_session": True}
    return await asyncio.create_subprocess_exec(
        launch.resolve_program(command, env),
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        limit=MAX_LINE_BYTES,
        creationflags=subproc.NO_WINDOW,
        **kwargs,
    )


class StderrTail:
    """The last few KiB a server wrote to stderr, with its secrets blanked."""

    def __init__(self, secrets: list[str] | None = None, limit: int = STDERR_TAIL_BYTES) -> None:
        self._buf = bytearray()
        self._limit = limit
        # Only values long enough to be a credential; blanking "1" would
        # shred the text while protecting nothing.
        self._secrets = [s for s in (secrets or []) if len(s) >= 6]

    async def drain(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while chunk := await stream.read(4096):
                self._buf += chunk
                if len(self._buf) > self._limit:
                    del self._buf[: len(self._buf) - self._limit]

    def text(self, max_chars: int = 600) -> str:
        text = self._buf.decode("utf-8", errors="replace").strip()
        for secret in self._secrets:
            text = text.replace(secret, "••••••••")
        return text[-max_chars:]


async def shutdown(proc: asyncio.subprocess.Process | None) -> None:
    """End the server and everything it started. Never raises."""
    if proc is None:
        return
    if proc.stdin is not None:
        with contextlib.suppress(Exception):
            proc.stdin.close()
    if not await _exited(proc):
        await _signal_tree(proc.pid, hard=False)
        if not await _exited(proc):
            await _signal_tree(proc.pid, hard=True)
            await _exited(proc)
    if not subproc.IS_WINDOWS:
        # The leader is gone; anything it started is an orphan in its group.
        await _signal_tree(proc.pid, hard=True)


async def _exited(proc: asyncio.subprocess.Process) -> bool:
    if proc.returncode is not None:
        return True
    try:
        await asyncio.wait_for(proc.wait(), GRACE_S)
    except TimeoutError:
        return False
    return True


async def _signal_tree(pid: int, *, hard: bool) -> None:
    if subproc.IS_WINDOWS:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                subproc.run, ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
