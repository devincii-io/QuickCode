"""Bash tool: run a shell command.

Executes a shell command and returns its combined stdout/stderr. Use this for
anything Read/Write/Edit/Glob/Grep don't cover — running tests, git, package
managers, etc. Prefers Git Bash on Windows (falls back to PowerShell if Git
Bash isn't installed); on other platforms uses ``/bin/bash``. The working
directory persists across calls within a session (a bare ``cd <dir>`` updates
it without spawning a subprocess). Output is capped at 30000 characters
(head+tail kept, middle elided).

Commands run inside a real pseudo-terminal (ConPTY on Windows via ``pywinpty``,
``pty`` on POSIX) so programs see a tty — colors, tty semantics, and correct
process-tree kill on timeout. If the PTY backend is unavailable the tool falls
back to a plain subprocess. Background execution is not yet supported.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.pty.session import PtySession
from quickcode.tools.base import Tool, ToolCtx, ToolResult

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
MAX_OUTPUT_CHARS = 30_000

_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]

_LONE_CD_RE = re.compile(r'^cd\s+(".*"|\'.*\'|\S+)\s*$')


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
    Input = BashInput

    def render_call(self, input: BashInput) -> str:  # noqa: A002
        label = input.description or input.command
        return f"⏺ Bash: {label}"

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

        # Preferred path: a real pseudo-terminal. Fall back to a plain
        # subprocess on any PTY error (backend missing, spawn failure, ...).
        try:
            session = PtySession(argv, cwd=str(cwd))
            raw_out, returncode, timed_out = await asyncio.to_thread(session.run, timeout_s)
            text = _clean_pty_output(raw_out)
        except Exception:  # noqa: BLE001 - any PTY failure -> subprocess fallback
            return await asyncio.to_thread(
                _run_subprocess, argv, str(cwd), timeout_s, timeout_ms, ctx
            )

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


def _clean_pty_output(raw: bytes) -> str:
    """Decode PTY bytes once and strip terminal control noise for the model."""
    text = raw.decode("utf-8", errors="surrogateescape")
    text = _ANSI_RE.sub("", text)
    # ConPTY uses CRLF line endings; normalize (and lone CR from carriage
    # returns used for in-place redraws).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _run_subprocess(
    argv: list[str], cwd: str, timeout_s: float, timeout_ms: int, ctx: ToolCtx
) -> ToolResult:
    """Fallback path when the PTY backend is unavailable. Plain pipes."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return ToolResult(content=f"Error: failed to start command: {exc}", is_error=True)

    try:
        raw_out, _ = proc.communicate(timeout=timeout_s)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid, ctx)
        try:
            raw_out, _ = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            raw_out = b""
        text = raw_out.decode("utf-8", errors="surrogateescape") if raw_out else ""
        text = _cap(text)
        msg = f"Error: command timed out after {timeout_ms}ms and was killed.\n{text}"
        return ToolResult(content=msg, is_error=True)

    text = raw_out.decode("utf-8", errors="surrogateescape") if raw_out else ""
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
            subprocess.run(
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
