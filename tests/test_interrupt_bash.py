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
import os
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools import bash as bash_mod
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import BashTool
from quickcode.tools.registry import default_registry
from tests.conftest import await_until


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


# How long the cancelled command sleeps before it would write its "I survived"
# marker. It is the window the process-tree kill has to land in: the cancel
# itself returns in well under half of this, so there is real headroom, and a
# kill that silently stopped working would miss it every time.
_GRACE_S = 1.5


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cancelled_mid_command(tmp_path_factory):
    """Start a long `bash` command, cancel it once it is genuinely running, and
    hand both observations to the tests below.

    One spawn, shared, because spawning is the expensive part and neither test
    is about spawning. `bash -lc` is a *login* shell: it sources the profile,
    which costs a few seconds on Windows before the command inside it starts to
    run at all. The previous version of these tests slept a flat second and then
    cancelled — which on this platform meant cancelling before the shell had
    reached the command, so "the interrupt was prompt" was measuring an
    interrupt of nothing. Waiting for the command's own marker is what makes the
    claim real, and paying for it once is what keeps it affordable.
    """
    tmp_path = tmp_path_factory.mktemp("interrupt")
    running = tmp_path / "running.txt"
    marker = tmp_path / "still-running.txt"
    # Announce the start; sleep long enough to be interrupted mid-write-window;
    # write the marker only if this survives the cancel; then keep sleeping, so
    # the command's total wanted runtime is far longer than any acceptable
    # cancel. The tool timeout is far above all of it: if the cancel is not
    # honoured, this hangs until pytest-timeout kills the run.
    command = (
        f'echo up > "{running.as_posix()}"; '
        f'sleep {_GRACE_S}; echo late > "{marker.as_posix()}"; '
        f"sleep 30"
    )
    tool = BashTool()
    task = asyncio.create_task(
        tool.run(tool.Input(command=command, timeout_ms=120_000), ctx_for(tmp_path))
    )
    assert await await_until(running.exists, timeout_s=60), "the command never started"
    command_started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancel_took = time.monotonic() - command_started
    # Sit out the rest of the sleep the command was in, plus a margin for the
    # write itself, so "the marker is absent" means killed rather than not-yet.
    await asyncio.sleep(max(_GRACE_S - cancel_took, 0) + 0.5)
    yield cancel_took, marker


async def test_interrupting_a_running_command_stops_it_promptly(cancelled_mid_command) -> None:
    """The observable claim: cancelling the tool returns in about as long as it
    takes to kill a process, not in as long as the command wanted to run."""
    cancel_took, _marker = cancelled_mid_command
    assert cancel_took < 10, "cancelling the tool did not return promptly"


async def test_a_cancelled_command_leaves_no_process_behind(cancelled_mid_command) -> None:
    """Killing the tree is the point — an abandoned `find /` would keep the disk
    busy for minutes after the turn ended, with nothing left to stop it."""
    _cancel_took, marker = cancelled_mid_command
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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


@pytest.fixture(params=["pty", "pipes"])
def command_path(request, monkeypatch):
    """Both ways a command can run on POSIX: the pty, and the plain-pipe
    fallback taken when the pty cannot be had."""
    if request.param == "pipes":
        monkeypatch.setattr(bash_mod, "_use_pty", lambda ctx: False)
    return request.param


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process groups")
async def test_stopping_a_command_also_stops_what_it_started(tmp_path, command_path) -> None:
    """The pipe fallback killed only the shell. `sleep 100 &` (or a dev
    server) was reparented to init and kept running, and because it still
    held the output pipe the worker thread sat in communicate() with it."""
    pidfile = tmp_path / "bg.pid"
    command = f'sleep 100 & echo $! > "{pidfile.as_posix()}"; wait'
    tool = BashTool()
    task = asyncio.create_task(
        tool.run(tool.Input(command=command, timeout_ms=120_000), ctx_for(tmp_path))
    )
    assert await await_until(lambda: pidfile.exists() and pidfile.read_text().strip() != "")
    child = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert await await_until(lambda: not _alive(child), timeout_s=5), (
            f"the background job outlived the interrupt ({command_path})"
        )
    finally:
        if _alive(child):
            os.kill(child, 9)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process groups")
async def test_a_timed_out_command_takes_its_children_with_it(tmp_path, command_path) -> None:
    pidfile = tmp_path / "bg.pid"
    command = f'sleep 100 & echo $! > "{pidfile.as_posix()}"; wait'
    tool = BashTool()
    started = time.monotonic()
    result = await tool.run(tool.Input(command=command, timeout_ms=1500), ctx_for(tmp_path))
    took = time.monotonic() - started
    child = int(pidfile.read_text())
    try:
        assert result.is_error and "timed out" in result.content
        assert await await_until(lambda: not _alive(child), timeout_s=5), (
            f"the background job outlived the timeout ({command_path})"
        )
        # A child still holding the pipe made the fallback wait out its own
        # five-second grace on top of the timeout.
        assert took < 4.5, f"the timeout took {took:.1f}s to come back"
    finally:
        if _alive(child):
            os.kill(child, 9)
