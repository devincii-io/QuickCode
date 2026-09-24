"""Starting a program by name, the way Windows needs it done safely.

Two Windows facts drive this module, and both are testable anywhere because
the resolver takes the platform's rules as arguments:

* ``CreateProcess`` appends only ``.exe``. ``npx``, ``npm`` and ``yarn`` are
  ``.cmd`` shims, so ``"command": "npx"`` -- the most common MCP config there
  is -- failed to start on the platform QuickCode targets first.
* A ``.cmd``/``.bat`` target runs under ``cmd.exe``, which re-parses the whole
  command line. ``subprocess`` quotes for the C runtime, not for cmd, so a
  value holding ``" & calc & "`` becomes a second command (BatBadBut).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from quickcode.kernel.authoring import discovery
from quickcode.security import launch
from tests.test_authoring import echo_tool, run_tool, write

WIN_EXT = ".COM;.EXE;.BAT;.CMD"


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_a_cmd_shim_is_found_through_pathext(tmp_path):
    shim = _touch(tmp_path / "node" / "npx.cmd")
    found = launch.resolve_program(
        "npx", {"PATH": str(shim.parent)}, windows=True, pathext=WIN_EXT, pathsep=os.pathsep)
    assert found == str(shim)


def test_the_current_directory_is_never_searched(tmp_path, monkeypatch):
    """shutil.which prepends '.' on Windows; a cloned repo holding npx.cmd at
    its root would otherwise run instead of the real one."""
    decoy = tmp_path / "repo"
    _touch(decoy / "npx.cmd")
    real = _touch(tmp_path / "node" / "npx.cmd")
    monkeypatch.chdir(decoy)
    found = launch.resolve_program(
        "npx", {"PATH": str(real.parent)}, windows=True, pathext=WIN_EXT, pathsep=os.pathsep)
    assert found == str(real)


def test_an_exe_wins_over_a_cmd_in_the_same_directory(tmp_path):
    _touch(tmp_path / "tool.cmd")
    exe = _touch(tmp_path / "tool.exe")
    assert launch.resolve_program(
        "tool", {"PATH": str(tmp_path)}, windows=True, pathext=WIN_EXT,
        pathsep=os.pathsep) == str(exe)


def test_paths_and_other_platforms_are_left_alone(tmp_path):
    _touch(tmp_path / "npx.cmd")
    env = {"PATH": str(tmp_path)}
    assert launch.resolve_program("npx", env, windows=False) == "npx"
    assert launch.resolve_program("./npx", env, windows=True, pathext=WIN_EXT) == "./npx"
    assert launch.resolve_program("missing", env, windows=True, pathext=WIN_EXT) == "missing"


@pytest.mark.parametrize("program,batch", [
    ("C:/node/NPX.CMD", True), ("run.bat", True), ("tool.exe", False), ("git", False),
])
def test_batch_targets_are_recognised(program, batch):
    assert launch.is_batch(program) is batch


@pytest.mark.parametrize("value", ['a" & calc & "', "%PATH%", "x|y", "a\nb", "!x!", "a^b"])
def test_values_cmd_would_reinterpret_are_flagged(value):
    assert launch.batch_unsafe(value)


@pytest.mark.parametrize("value", ["src/app.py", "file (1).txt", "a b c", "C:\\x\\y"])
def test_ordinary_values_are_not(value):
    assert not launch.batch_unsafe(value)


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


def test_a_command_tool_refuses_to_hand_cmd_a_second_command(project):
    write(project, "echo-args", echo_tool(
        ["./check.cmd", "{target}"],
        [{"name": "target", "type": "string", "required": True}]))
    tool = discovery.discover(project).plugins[0].to_tool()
    result = run_tool(tool, project, target='x" & calc.exe & "')
    assert result.is_error
    assert "cmd.exe" in result.content
    assert "argv" not in (result.ui_meta or {})


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX script stands in for the shim")
def test_a_plain_value_still_reaches_a_batch_named_target(project):
    script = project / "check.cmd"
    script.write_text(
        f"#!{sys.executable}\nimport sys, json\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8")
    script.chmod(0o755)
    write(project, "echo-args", echo_tool(
        ["./check.cmd", "{target}"],
        [{"name": "target", "type": "string", "required": True}]))
    tool = discovery.discover(project).plugins[0].to_tool()
    result = run_tool(tool, project, target="src/app.py")
    assert not result.is_error, result.content
    assert json.loads(result.content.strip()) == ["src/app.py"]
