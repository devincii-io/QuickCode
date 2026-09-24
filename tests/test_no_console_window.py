"""No command may put a console window on the user's screen.

``QuickCodeApp.exe`` is built windowed, so a console program it spawns has no
console to inherit and Windows allocates a fresh one -- visible, on top,
for as long as the command runs. The user's report was a terminal blinking at
them every few seconds.

This is invisible to every other test: the flag changes nothing about a
command's output, its exit code or its timing, only whether a window appears.
So it is asserted structurally, the same way the installer's `/T` is -- a
source-level rule, because the failure it prevents cannot be observed from
inside a test process. The rule is broader than the flag: ``subproc`` is also
where a child's environment loses QuickCode's API keys, its stdin becomes the
null device and it gets a process group ``kill_tree`` can end. So any spawn
anywhere else fails here, whether or not it remembered ``CREATE_NO_WINDOW``.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from quickcode import subproc

ROOT = Path(__file__).resolve().parents[1] / "quickcode"

# Where a raw spawn is still correct, and why.
ALLOWED = {
    # The helper itself: this is the one place that starts a process.
    "quickcode/subproc.py",
    # The installer has its own window and is meant to be seen. It is also
    # deliberately detached, and must keep the environment the app was
    # started with: it relaunches the app. See `launch_installer`.
    "quickcode/update.py",
}

# Every stdlib entry point that starts a program, by the module it lives in.
_SPAWNS: dict[str, set[str]] = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output",
                   "getoutput", "getstatusoutput"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "asyncio.subprocess": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
           "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
           "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
           "spawnvpe"},
    "pty": {"spawn", "fork"},
}
# Reached through an object rather than a module: `loop.subprocess_exec(...)`.
_ANY_RECEIVER = {"create_subprocess_exec", "create_subprocess_shell",
                 "subprocess_exec", "subprocess_shell"}


def spawn_calls(source: str) -> list[int]:
    """The lines of ``source`` that start a program without ``subproc``.

    An AST walk rather than a regex, so a docstring that names
    ``create_subprocess_exec`` is not a spawn, and ``import subprocess as sp``
    or ``from subprocess import Popen`` is not a way around the rule. A name
    imported only for a type annotation is fine until something calls it.
    """
    tree = ast.parse(source)
    modules: dict[str, str] = {}  # local name -> module it names
    functions: dict[str, str] = {}  # local name -> the spawn function it is
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name in _SPAWNS.get(node.module, ()):
                    functions[alias.asname or alias.name] = alias.name
                elif f"{node.module}.{alias.name}" in _SPAWNS:
                    modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def dotted(expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Name):
            return modules.get(expr.id)
        if isinstance(expr, ast.Attribute):
            base = dotted(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in functions:
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute):
            owner = dotted(func.value)
            if func.attr in _ANY_RECEIVER or func.attr in _SPAWNS.get(owner or "", ()):
                lines.append(node.lineno)
    return sorted(lines)


def _sources() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_module_starts_a_program_except_through_subproc() -> None:
    offenders: list[str] = []
    for path in _sources():
        rel = path.relative_to(ROOT.parent).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        source_lines = text.splitlines()
        for n in spawn_calls(text):
            offenders.append(f"{rel}:{n}: {source_lines[n - 1].strip()}")
    assert not offenders, (
        "these start a program without quickcode.subproc (no CREATE_NO_WINDOW, "
        "QuickCode's API keys in the child's environment, no process group to "
        "kill); use subproc.run, subproc.spawn or subproc.spawn_async:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("snippet", [
    "import subprocess\nsubprocess.run(['x'])",
    "import subprocess as sp\nsp.Popen(['x'])",
    "from subprocess import check_output\ncheck_output(['x'])",
    "from subprocess import Popen as P\nP(['x'])",
    "import asyncio\nasyncio.create_subprocess_exec('x')",
    "import asyncio\nasyncio.subprocess.create_subprocess_shell('x')",
    "from asyncio import create_subprocess_exec\ncreate_subprocess_exec('x')",
    "from asyncio import subprocess as aio_sub\naio_sub.create_subprocess_exec('x')",
    "loop.subprocess_exec(factory, 'x')",
    "import os\nos.system('x')",
    "import os\nos.execvp('x', ['x'])",
    "import os\nos.posix_spawn('x', ['x'], {})",
    "import pty\npty.spawn(['x'])",
])
def test_the_scan_sees_every_way_of_starting_a_program(snippet: str) -> None:
    assert spawn_calls(snippet), snippet


@pytest.mark.parametrize("snippet", [
    '"""Spawned with ``asyncio.create_subprocess_exec``."""',
    "# subprocess.run(['x'])",
    "from subprocess import Popen\nproc: Popen | None = None",
    "from quickcode import subproc\nsubproc.run(['git'])\nsubproc.spawn(['x'])",
    "import os\nos.kill(1, 0)\nos.killpg(1, 9)",
])
def test_the_scan_leaves_alone_what_is_not_a_spawn(snippet: str) -> None:
    assert spawn_calls(snippet) == [], snippet


def test_the_helper_asks_windows_for_no_console() -> None:
    assert subproc.NO_WINDOW == (subprocess.CREATE_NO_WINDOW if subproc.IS_WINDOWS else 0)


def test_every_helper_passes_the_flag(monkeypatch, tmp_path) -> None:
    """Checked by substitution, so it runs here too: on POSIX the real flag is
    zero, which would make the assertion vacuous."""
    flag = 0x0800_0000
    seen: list[int] = []

    def fake_sync(argv, **kw):
        seen.append(kw["creationflags"])

    async def fake_async(*argv, **kw):
        seen.append(kw["creationflags"])

    monkeypatch.setattr(subproc, "NO_WINDOW", flag)
    monkeypatch.setattr(subproc.subprocess, "run", fake_sync)
    monkeypatch.setattr(subproc.subprocess, "Popen", fake_sync)
    monkeypatch.setattr(subproc.asyncio, "create_subprocess_exec", fake_async)

    subproc.run([sys.executable])
    subproc.spawn([sys.executable], cwd=str(tmp_path))
    asyncio.run(subproc.spawn_async([sys.executable], cwd=str(tmp_path)))

    assert seen == [flag, flag, flag]


@pytest.mark.skipif(not subproc.IS_WINDOWS, reason="a console window is a Windows problem")
def test_the_helper_keeps_a_caller_s_own_flags() -> None:
    """It ORs rather than replaces, so a caller can still detach a child."""
    seen: dict = {}

    def fake(argv, **kw):
        seen.update(kw)
        return None

    original = subprocess.Popen
    subprocess.Popen = fake  # type: ignore[assignment]
    try:
        subproc.spawn([sys.executable], creationflags=subprocess.DETACHED_PROCESS)
    finally:
        subprocess.Popen = original  # type: ignore[assignment]

    assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert seen["creationflags"] & subprocess.DETACHED_PROCESS
