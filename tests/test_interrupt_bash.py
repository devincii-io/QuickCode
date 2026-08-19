"""Stop has to stop a command that is still running.

A mutating tool was awaited outright while the read-only batch raced the cancel
event, so pressing Stop during a long `bash` set a flag and nothing else: the
transcript printed "(interrupt requested)" once per press while the loop sat
inside the command until its own timeout — minutes, for something like
`find / -name "*x*"`. Cancelling the coroutine is not enough on its own either,
because the command runs in a worker thread and the child process outlives it;
the tool kills the process tree on its way out.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import BashTool
from quickcode.tools.registry import default_registry


def ctx_for(tmp_path: Path) -> ToolCtx:
    return ToolCtx(
        cwd=tmp_path,
        read_registry=ReadRegistry(),
        shell_name="bash",
        platform=sys.platform,
        extra={},
    )


def test_bash_declares_itself_interruptible_and_the_writers_do_not() -> None:
    """The flag is what the loop reads to decide whether Stop may cut a call
    off. `write` and `edit` stay uninterruptible on purpose: a file truncated
    halfway is worse than an interrupt that waits for the write to land."""
    registry = default_registry()
    assert registry.get("bash").interruptible is True
    for name in ("write", "edit"):
        assert registry.get(name).interruptible is False, name


async def test_interrupting_a_running_command_stops_it_promptly(tmp_path: Path) -> None:
    """The observable claim: cancelling the tool returns in about as long as it
    takes to kill a process, not in as long as the command wanted to run."""
    tool = BashTool()
    ctx = ctx_for(tmp_path)
    # 30 s of sleeping, with a tool timeout far above it: if the cancel is not
    # honoured this test hangs until pytest-timeout kills the run.
    task = asyncio.create_task(
        tool.run(tool.Input(command="sleep 30", timeout_ms=120_000), ctx)
    )
    await asyncio.sleep(1.0)          # let it actually start
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 10, "cancelling the tool did not return promptly"


async def test_a_cancelled_command_leaves_no_process_behind(tmp_path: Path) -> None:
    """Killing the tree is the point — an abandoned `find /` would keep the disk
    busy for minutes after the turn ended, with nothing left to stop it."""
    marker = tmp_path / "still-running.txt"
    # Writes the marker only if it survives past the cancel.
    command = f'sleep 3; echo late > "{marker.as_posix()}"'
    tool = BashTool()
    task = asyncio.create_task(
        tool.run(tool.Input(command=command, timeout_ms=120_000), ctx_for(tmp_path))
    )
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(4.0)          # well past when the command would have written
    assert not marker.exists(), "the command outlived its cancellation"


def test_a_protected_path_in_a_command_no_longer_stops_a_yolo_run(tmp_path: Path) -> None:
    """The other half of the same complaint: approving anything in yolo.

    `bash` treats every non-option token as a possible path, so the `/` in
    `find /` resolved outside the project, hit the protected-path rule, and
    prompted — in the one mode whose entire promise is that it does not.
    """
    engine = PermissionEngine(mode=Mode.yolo, rules=Rules(), root=tmp_path)
    from quickcode.core.permissions import Decision

    assert engine.evaluate("bash", 'find / -name "*nimocam*" -type f 2>/dev/null') == (
        Decision.allow
    )
