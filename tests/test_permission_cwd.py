"""A shell's relative paths are relative to where its last `cd` left it.

The bash tool persists a lone `cd` across calls (`ToolCtx.extra["bash_cwd"]`),
and the engine resolved every relative path against the project root anyway.
After one approved `cd ..`, the engine believed it was still in the project:
`cat notes.txt` auto-allowed as a read-only builtin, and `rm -rf *` under a
`bash(rm **)` rule emptied the directory above the project without a word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import AssembledToolCall
from quickcode.core.history import History
from quickcode.core.loop import _run_tool
from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (tmp_path / "notes.txt").write_text("not the project's", encoding="utf-8")
    return root


def test_a_shell_standing_outside_the_project_asks_for_everything(project):
    outside = project.parent
    assert engine(root=project).evaluate("bash", "cat notes.txt", cwd=outside) == Decision.ask
    assert engine(root=project).evaluate("bash", "ls", cwd=outside) == Decision.ask
    assert engine(Mode.dontask, root=project).evaluate(
        "bash", "cat notes.txt", cwd=outside
    ) == Decision.deny
    # Yolo does not ask about protected paths, and this is one.
    assert engine(Mode.yolo, root=project).evaluate(
        "bash", "cat notes.txt", cwd=outside
    ) == Decision.allow


def test_an_allow_rule_does_not_follow_the_shell_out_of_the_project(project):
    e = engine(root=project, allow=["bash(rm **)"])
    assert e.evaluate("bash", "rm -rf *", cwd=project.parent) == Decision.ask
    assert e.evaluate("bash", "rm -rf *", cwd=project) == Decision.allow


def test_a_shell_inside_the_project_resolves_from_where_it_stands(project):
    e = engine(root=project)
    assert e.evaluate("bash", "cat a.py", cwd=project / "src") == Decision.allow
    assert e.evaluate("bash", "cat ../.git/config", cwd=project / "src") == Decision.ask


def test_a_shell_standing_in_a_protected_directory_asks(project):
    assert engine(root=project).evaluate("bash", "cat config", cwd=project / ".git") == (
        Decision.ask
    )


async def test_the_loop_hands_the_engine_the_shell_it_moved(project):
    """End to end: the tool moves the shell, the loop tells the engine."""
    asked: list[str] = []

    async def record(request):
        asked.append(request.arg)
        return PermissionOutcome(allow=False)

    ctx = ToolCtx(cwd=project, read_registry=ReadRegistry(), extra={})
    agent = AgentInstance(
        name="main",
        provider=None,
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(Mode.ask, Rules(), project),
        model="test/model",
        permission_cb=record,
    )
    ctx.extra["bash_cwd"] = project / "src"
    content, is_error, _ = await _run_tool(
        agent, AssembledToolCall("1", "bash", '{"command": "cat a.py"}')
    )
    assert asked == []
    ctx.extra["bash_cwd"] = project.parent
    content, is_error, _ = await _run_tool(
        agent, AssembledToolCall("2", "bash", '{"command": "cat notes.txt"}')
    )
    assert asked == ["cat notes.txt"]
    assert is_error and "Permission denied" in content


@pytest.mark.parametrize("command", [
    "cd && cat .bash_history", "cd; cat .aws/credentials", "cd - && ls", "cd -- && ls",
    "cd -P && ls", "cd",
])
def test_a_cd_that_names_no_directory_leaves_the_project(command, project):
    """A bare `cd` goes home and `cd -` goes back; the rest of the line reads
    there, while every relative path in it was resolved against the project.
    Both builtins are read-only, so this ran unprompted in every mode."""
    assert engine(root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.plan, root=project).evaluate("bash", command) == Decision.ask
    assert engine(Mode.dontask, root=project).evaluate("bash", command) == Decision.deny


def test_a_cd_into_the_project_stays_unprompted(project):
    assert engine(root=project).evaluate("bash", "cd src && ls") == Decision.allow
