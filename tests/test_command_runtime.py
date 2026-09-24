"""Running an authored command tool: the process, not the template.

``test_authoring.py`` covers what argv a template resolves to. This covers
what happens once that argv is handed to the OS: that it gets no console
window on Windows, that a timeout (or the turn being torn down) kills the
whole process tree rather than the one process at the top of it, and that a
batch file -- which Windows runs through cmd.exe, re-parsing every argument --
is refused rather than becoming the shell the module promises not to have.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from quickcode import subproc
from quickcode.kernel.authoring import discovery
from quickcode.tools import command as command_module
from quickcode.tools.base import ReadRegistry, ToolCtx


@pytest.fixture
def project(tmp_path, monkeypatch):
    import quickcode.config as config_module
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    root = tmp_path / "proj"
    (root / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    return root


def tool_for(project: Path, argv: list[str], params: list[dict] | None = None, **front):
    lines = ["---", "kind: tool", "name: probe", "description: A probe."]
    lines += [f"{k}: {v}" for k, v in front.items()]
    lines += ["---", "", "```json params", json.dumps(params or []), "```", "",
              "```json argv", json.dumps(argv), "```", ""]
    (project / ".quickcode" / "plugins" / "probe.md").write_text("\n".join(lines),
                                                                  encoding="utf-8")
    found = discovery.discover(project)
    assert [p.id for p in found.plugins] == ["tool.probe"], found
    return found.plugins[0].to_tool()


async def run(tool, project: Path, **values):
    ctx = ToolCtx(cwd=project, read_registry=ReadRegistry())
    return await tool.run(tool.Input(**values), ctx)


class _FakeProc:
    pid = 4242
    returncode = 0

    async def communicate(self, _input=None):
        return b"[]\n", b""

    async def wait(self):
        return 0


async def test_a_command_tool_asks_for_no_console_window(project, monkeypatch):
    """Every other spawn site goes through ``subproc`` for CREATE_NO_WINDOW;
    this one called asyncio directly, so the windowed app flashed a console
    for every authored tool it ran."""
    seen: dict = {}

    async def fake_exec(*argv, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(command_module.subproc, "NO_WINDOW", 0x0800_0000)
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", fake_exec)
    tool = tool_for(project, ["python", "-c", "pass"])

    await run(tool, project)

    assert seen["creationflags"] & 0x0800_0000
    # Its own process group, so a timeout can take the whole tree down.
    assert seen["start_new_session"] is True


@pytest.mark.parametrize("resolved", [r"C:\nodejs\npm.CMD", r"C:\proj\build.bat"])
async def test_a_batch_file_is_refused_on_windows(project, monkeypatch, resolved):
    """cmd.exe re-parses a batch file's arguments, so a parameter holding
    ``& calc`` would run -- the injection the argv design exists to rule out."""
    spawned: list = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        return _FakeProc()

    monkeypatch.setattr(command_module.subproc, "IS_WINDOWS", True)
    monkeypatch.setattr(command_module.shutil, "which", lambda name, path=None: resolved)
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", fake_exec)
    tool = tool_for(project, ["npm", "run", "{script}"],
                    [{"name": "script", "type": "string", "required": True}])

    result = await run(tool, project, script="test & calc")

    assert result.is_error
    assert "batch file" in result.content
    assert spawned == []


async def test_the_same_program_on_posix_is_not_second_guessed(project, monkeypatch):
    async def fake_exec(*argv, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(command_module.subproc, "IS_WINDOWS", False)
    monkeypatch.setattr(command_module.shutil, "which", lambda name, path=None: "/x/npm.cmd")
    monkeypatch.setattr(command_module.asyncio, "create_subprocess_exec", fake_exec)
    tool = tool_for(project, ["npm", "test"])

    result = await run(tool, project)

    assert not result.is_error, result.content


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie nobody has reaped yet (a
    container's PID 1 often reaps nothing)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().split(") ", 1)[1][:1] != "Z"
    return True


SPAWNS_A_GRANDCHILD = (
    "import subprocess, sys, time, pathlib;"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "pathlib.Path('grandchild.pid').write_text(str(p.pid));"
    "time.sleep(60)"
)


@pytest.mark.skipif(sys.platform == "win32", reason="checks POSIX process groups")
async def test_a_timeout_kills_the_whole_tree(project):
    """``proc.kill()`` reached the direct child only: a test runner's workers,
    or anything a script started, kept running after the tool gave up."""
    tool = tool_for(project, [sys.executable, "-c", SPAWNS_A_GRANDCHILD], timeout_ms=1500)

    result = await run(tool, project)

    assert result.is_error and "timed out" in result.content
    pid = int((project / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="checks POSIX process groups")
async def test_a_cancelled_run_does_not_leave_its_process_behind(project):
    """Closing the conversation cancels the task awaiting the tool. The child
    used to be abandoned mid-communicate and run on, unowned."""
    tool = tool_for(project, [sys.executable, "-c", SPAWNS_A_GRANDCHILD])

    task = asyncio.create_task(run(tool, project))
    marker = project / "grandchild.pid"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _alive(pid)


async def test_a_missing_working_directory_is_named_as_such(project):
    """cwd: file_dir with a path whose directory does not exist failed as
    "could not start 'python'", blaming a program that was never the problem."""
    tool = tool_for(project, [sys.executable, "-c", "pass", "{target}"],
                    [{"name": "target", "type": "path", "required": True}],
                    cwd="file_dir")

    result = await run(tool, project, target="no/such/dir/file.txt")

    assert result.is_error
    assert "working directory" in result.content
