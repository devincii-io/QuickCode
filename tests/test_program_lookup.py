"""A program QuickCode names is the one on PATH, never one the repository holds.

On Windows a bare program name is looked for in the current directory before
``PATH`` -- by ``CreateProcess`` and by ``shutil.which`` alike -- and
QuickCode's current directory is wherever it was started: the repository, for
``qc`` run in a terminal there or ``-p``. So a ``git.exe`` or ``rg.exe``
committed to a repository ran the moment the project opened (git) or the model
searched it (the auto-allowed ``grep``), before anyone trusted it.

This machine is not Windows, so the resolver takes the platform's rules as
arguments and the tests pass Windows' own: a planted program in the current
directory, a real one in a ``PATH`` directory, and the lookup must pick the
real one. The last tests are source-level, like ``test_no_console_window``:
no spawn may reach the OS with a bare name the resolver never saw.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from quickcode import gitcmd, subproc
from quickcode.security import launch
from quickcode.tools import grep as grep_module
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash import _find_git_bash
from tests.test_no_console_window import _ANY_RECEIVER, _SPAWNS, ALLOWED, ROOT, spawn_calls

WIN_EXT = ".COM;.EXE;.BAT;.CMD"


def _plant(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A repository holding look-alikes, and the real programs in a PATH directory.

    QuickCode's current directory is the repository, as it is for ``qc`` run
    in one.
    """
    repo = tmp_path / "repo"
    real = tmp_path / "Program Files" / "Git" / "cmd"
    for name in ("git.exe", "rg.exe", "bash.exe", "powershell.exe", "taskkill.exe"):
        _plant(repo / name)
        _plant(real / name)
    _plant(repo / "git.bat")
    _plant(repo / "git.com")
    monkeypatch.chdir(repo)
    return repo, real


def win(name: str, path: str, **kw) -> str:
    return subproc.resolve_program(name, env={"PATH": path, "PATHEXT": WIN_EXT},
                                   windows=True, **kw)


# ---- the lookup, with Windows' rules ------------------------------------------


@pytest.mark.parametrize("name", ["git", "rg", "bash", "powershell", "taskkill"])
def test_the_path_program_wins_over_one_planted_in_the_current_directory(machine, name):
    repo, real = machine
    assert win(name, str(real)) == str(real / f"{name}.exe")


@pytest.mark.parametrize("before", [".", "", ".\\", "bin", "repo", '"."'])
def test_a_relative_path_entry_is_never_searched(machine, before):
    """``.`` is the current directory spelled out, and an empty entry is read
    the same way; any relative entry depends on where QuickCode was started."""
    repo, real = machine
    assert win("git", f"{before};{real}") == str(real / "git.exe")


def test_an_entry_that_is_the_current_directory_is_skipped_too(machine):
    repo, real = machine
    assert win("git", f"{repo};{real}") == str(real / "git.exe")


def test_an_entry_that_is_the_project_is_skipped_wherever_quickcode_started(
        machine, tmp_path, monkeypatch):
    repo, real = machine
    monkeypatch.chdir(tmp_path)
    assert win("git", f"{repo};{real}", cwd=repo) == str(real / "git.exe")


def test_a_batch_file_is_never_what_a_system_tool_resolves_to(machine, tmp_path):
    """``git.bat`` would run under cmd.exe, which re-parses every argument."""
    only_batch = tmp_path / "shims"
    _plant(only_batch / "git.bat")
    _plant(only_batch / "git.cmd")
    with pytest.raises(subproc.ProgramNotFound):
        win("git", str(only_batch))
    with pytest.raises(subproc.ProgramNotFound):
        win("git.bat", str(only_batch))
    _plant(only_batch / "git.exe")
    batch_first = {"PATH": str(only_batch), "PATHEXT": ".BAT;.CMD;.EXE"}
    assert subproc.resolve_program("git", env=batch_first, windows=True) == str(
        only_batch / "git.exe")


def test_a_program_found_nowhere_is_a_clear_error_not_a_guess(machine, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(subproc.ProgramNotFound) as caught:
        win("rg", f".;{empty}")
    assert isinstance(caught.value, OSError)  # every spawn site already handles those
    message = str(caught.value)
    assert "'rg'" in message and "PATH" in message
    assert "current directory" in message


def test_the_environment_s_path_is_read_whatever_its_case(machine):
    repo, real = machine
    env = {"Path": str(real), "PathExt": WIN_EXT}
    assert subproc.resolve_program("git", env=env, windows=True) == str(real / "git.exe")


@pytest.mark.parametrize("path", [r"C:\Git\bin\git.exe", "./git", "tools\\rg.exe", "C:git"])
def test_a_path_is_what_its_author_meant_and_is_left_alone(machine, path):
    assert win(path, "") == path


def test_a_command_tool_or_mcp_shim_skips_the_same_directories(machine, tmp_path):
    """``launch`` allows the ``.cmd`` shims npx and npm are, and nothing else
    about the lookup: the same relative and current-directory entries are out."""
    repo, real = machine
    _plant(repo / "npx.cmd")
    node = _plant(tmp_path / "node" / "npx.cmd")
    env = {"PATH": f".;{repo};{node.parent}", "PATHEXT": WIN_EXT}
    assert launch.resolve_program("npx", env, windows=True) == str(node)


# ---- the lookup on POSIX ------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH rules")
@pytest.mark.parametrize("before", ["", ".", "bin"])
def test_posix_skips_relative_entries_and_keeps_everything_else(machine, before):
    repo, real = machine
    _plant(repo / "tool")
    _plant(real / "tool")
    env = {"PATH": f"{before}:{real}"}
    assert subproc.resolve_program("tool", env=env, windows=False) == str(real / "tool")
    # An absolute entry is the user's own choice on POSIX, as it always was.
    assert subproc.resolve_program("tool", env={"PATH": str(repo)}, windows=False) == str(
        repo / "tool")
    with pytest.raises(subproc.ProgramNotFound):
        subproc.resolve_program("tool", env={"PATH": before}, windows=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH rules")
def test_posix_passes_over_a_file_that_is_not_executable(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    (first / "tool").parent.mkdir()
    (first / "tool").write_text("", encoding="utf-8")
    _plant(second / "tool")
    env = {"PATH": f"{first}:{second}"}
    assert subproc.resolve_program("tool", env=env, windows=False) == str(second / "tool")


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX script")
def test_a_real_spawn_runs_the_path_program_not_the_planted_one(tmp_path, monkeypatch):
    repo, real = tmp_path / "repo", tmp_path / "bin"
    (repo / "probe").parent.mkdir()
    (repo / "probe").write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    (repo / "probe").chmod(0o755)
    (real / "probe").parent.mkdir()
    (real / "probe").write_text("#!/bin/sh\necho real\n", encoding="utf-8")
    (real / "probe").chmod(0o755)
    monkeypatch.chdir(repo)
    env = subproc.child_env({"PATH": f".::{real}"})

    done = subproc.run(["probe"], capture_output=True, env=env, timeout=10)

    assert done.stdout.strip() == b"real"


# ---- every spawn resolves ----------------------------------------------------


@pytest.fixture
def windows_spawns(machine, monkeypatch):
    """subprocess and asyncio replaced by recorders, the resolver told it is on
    Windows, and the real programs on the environment's PATH."""
    repo, real = machine
    seen: list[str] = []

    def fake_sync(argv, **_kw):
        seen.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    async def fake_async(program, *_args, **_kw):
        seen.append(program)

    monkeypatch.setattr(subproc, "IS_WINDOWS", True)
    monkeypatch.setattr(subproc.subprocess, "run", fake_sync)
    monkeypatch.setattr(subproc.subprocess, "Popen", fake_sync)
    monkeypatch.setattr(subproc.asyncio, "create_subprocess_exec", fake_async)
    monkeypatch.setenv("PATH", f".;{repo};{real}")
    monkeypatch.setenv("PATHEXT", WIN_EXT)
    return seen, real


def test_run_spawn_and_spawn_async_start_the_path_program(windows_spawns):
    seen, real = windows_spawns
    subproc.run(["git", "status"])
    subproc.spawn(["powershell", "-NoProfile"], cwd=str(real.parent))
    asyncio.run(subproc.spawn_async(["bash", "-lc", "true"]))
    subproc.kill_tree(4242)  # taskkill, on Windows
    assert seen == [str(real / f"{n}.exe") for n in ("git", "powershell", "bash", "taskkill")]


def test_nothing_starts_when_the_program_is_nowhere(windows_spawns, monkeypatch, tmp_path):
    seen, _real = windows_spawns
    monkeypatch.setenv("PATH", ".")
    with pytest.raises(subproc.ProgramNotFound):
        subproc.run(["git", "status"])
    with pytest.raises(subproc.ProgramNotFound):
        subproc.spawn(["rg"])
    assert seen == []


def test_opening_a_project_runs_the_real_git(windows_spawns, machine, tmp_path, monkeypatch):
    """git runs when a project opens -- before any trust prompt -- and it is
    given the project with ``-C``, so the project is kept off PATH as well."""
    seen, real = windows_spawns
    repo, _ = machine
    monkeypatch.chdir(tmp_path)  # QuickCode started somewhere else entirely
    gitcmd.run(repo, "status")
    assert seen and set(seen) == {str(real / "git.exe")}


def test_git_missing_is_reported_not_raised(windows_spawns, machine, monkeypatch):
    repo, _ = machine
    monkeypatch.setenv("PATH", ".")
    result = gitcmd.run(repo, "rev-parse", "HEAD")
    assert result.code == -1
    assert "'git' was not found on PATH" in result.stderr


async def test_the_auto_allowed_grep_never_runs_the_repository_s_rg(windows_spawns, machine):
    seen, real = windows_spawns
    repo, _ = machine
    (repo / "a.txt").write_text("needle\n", encoding="utf-8")
    await grep_module.GrepTool().run(grep_module.GrepTool.Input(pattern="needle"),
                                     ToolCtx(cwd=repo, read_registry=ReadRegistry()))
    assert seen == [str(real / "rg.exe")]


def test_the_agent_s_shell_is_not_the_repository_s_bash(windows_spawns, machine):
    _seen, real = windows_spawns
    repo, _ = machine
    assert _find_git_bash(repo) == str(real / "bash.exe")


# ---- no way around it, by construction ---------------------------------------


def _sources() -> list[tuple[str, str]]:
    return [(p.relative_to(ROOT.parent).as_posix(), p.read_text(encoding="utf-8"))
            for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


_SPAWN_NAMES = set().union(*_SPAWNS.values(), _ANY_RECEIVER)


def _spawns_at(tree: ast.AST, lines: list[int]) -> list[ast.Call]:
    """The spawn calls on ``lines`` -- not the calls nested in their arguments."""
    def name(func: ast.expr) -> str:
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno in lines
            and name(n.func) in _SPAWN_NAMES]


def _is_call_to(node: ast.expr, name: str) -> bool:
    if isinstance(node, ast.Starred):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name)


def test_nothing_looks_a_program_up_with_shutil_which():
    """It searches the current directory first on Windows."""
    offenders = []
    for rel, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "shutil" and any(
                    a.name == "which" for a in node.names):
                offenders.append(f"{rel}:{node.lineno}")
            if (isinstance(node, ast.Attribute) and node.attr == "which"
                    and isinstance(node.value, ast.Name) and node.value.id == "shutil"):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "use subproc.find_program instead:\n  " + "\n  ".join(offenders)


def test_every_raw_spawn_in_subproc_takes_a_resolved_argv():
    """``subproc`` is the one module allowed to call subprocess and asyncio
    directly; each of those calls must be handed ``_resolved``'s argv."""
    text = (ROOT / "subproc.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    calls = _spawns_at(tree, spawn_calls(text))
    assert len(calls) >= 3, "run, spawn and spawn_async"
    bare = [c.lineno for c in calls if not (c.args and _is_call_to(c.args[0], "_resolved"))]
    assert not bare, f"subproc.py spawns without _resolved at lines {bare}"


def test_no_other_raw_spawn_names_a_bare_program():
    """The installer is the one other raw spawn, and it is handed a full path."""
    offenders = []
    for rel in sorted(ALLOWED - {"quickcode/subproc.py"}):
        text = (ROOT.parent / rel).read_text(encoding="utf-8")
        for call in _spawns_at(ast.parse(text), spawn_calls(text)):
            first = call.args[0] if call.args else None
            if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
                first = first.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                offenders.append(f"{rel}:{call.lineno}: {first.value!r}")
    assert not offenders, offenders


def test_a_pty_spawn_resolves_its_argv_first():
    """pywinpty looks a bare name up with ``shutil.which`` -- current directory
    first -- so a ConPTY spawn is handed ``subproc.resolve_argv``'s argv."""
    found, bare = 0, []
    for rel, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "spawn"):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Attribute) and owner.attr == "PtyProcess"
                    or isinstance(owner, ast.Name) and owner.id == "PtyProcess"):
                continue
            found += 1
            if not (node.args and _is_call_to(node.args[0], "resolve_argv")):
                bare.append(f"{rel}:{node.lineno}")
    assert found >= 2, "pty/session.py and pty/interactive.py"
    assert not bare, bare
