"""Reading back a subagent report must not cost a permission prompt.

``subagents/artifacts.py`` offloads an oversized subagent report to
``<project>/.quickcode/artifacts/<agent_id>.md`` and tells the parent, in the
tool result, to read that file for the rest. Every path with a ``.quickcode``
component is a protected path, so the parent was prompted -- in every mode,
yolo included -- for a file the session had just written itself. Four parallel
``read`` calls on four artifacts meant four modals.

The exception these tests pin is deliberately narrow: reads only, that one
directory only, resolved against the project root. Everything the protected
path check did before, it still does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.tools.read import ReadTool
from quickcode.tools.write import WriteTool

ARTIFACT = ".quickcode/artifacts/explore-1.md"
ALL_MODES = [Mode.plan, Mode.ask, Mode.auto_edit, Mode.dontask, Mode.yolo]


def engine(mode=Mode.ask, root=None, **kw) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**kw), root=root or Path.cwd())


@pytest.fixture
def project(tmp_path) -> Path:
    """A project root with the artifacts directory the subagents write to."""
    (tmp_path / ".quickcode" / "artifacts").mkdir(parents=True)
    (tmp_path / ".quickcode" / "artifacts" / "explore-1.md").write_text("report", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("mode", ALL_MODES)
def test_a_subagent_artifact_is_read_back_without_a_prompt_in_any_mode(mode, project):
    e = engine(mode, root=project)
    assert e.evaluate("read", ARTIFACT) == Decision.allow
    assert e.evaluate("read", str(project / ".quickcode" / "artifacts" / "explore-1.md")) == (
        Decision.allow
    )


def test_the_read_tool_itself_reads_an_artifact_without_a_prompt(project):
    """Through the tool's own declared spec, not a hand-written one."""
    decision, target = engine(root=project).evaluate_tool(
        ReadTool(), {"file_path": ARTIFACT}
    )
    assert decision == Decision.allow
    assert target == ARTIFACT


@pytest.mark.parametrize("mode", [Mode.ask, Mode.auto_edit])
def test_writing_to_the_artifacts_directory_still_prompts(mode, project):
    """The exception is read-only. A tool that mutates the same path is asked
    about exactly as before -- the agent writing its own report goes through
    ``artifacts.py``, not through a gated tool call. Yolo is not in the list
    because yolo does not prompt for anything; see the last test in this file."""
    e = engine(mode, root=project, allow=["write", "edit"])
    assert e.evaluate("write", ARTIFACT) == Decision.ask
    assert e.evaluate("edit", ARTIFACT) == Decision.ask
    decision, _ = e.evaluate_tool(WriteTool(), {"file_path": ARTIFACT, "content": "x"})
    assert decision == Decision.ask


def test_dontask_still_denies_a_write_to_the_artifacts_directory(project):
    assert engine(Mode.dontask, root=project).evaluate("write", ARTIFACT) == Decision.deny


@pytest.mark.parametrize("path", [".quickcode/settings.json", ".quickcode/settings.local.json"])
def test_the_rest_of_the_quickcode_directory_still_asks_for_reads(path, project):
    """The session's own configuration is the reason ``.quickcode`` is protected
    in the first place, and reading it is still a decision the user makes."""
    assert engine(root=project, allow=["read"]).evaluate("read", path) == Decision.ask
    assert engine(Mode.dontask, root=project).evaluate("read", path) == Decision.deny


@pytest.mark.parametrize("path", [".git/config", ".ssh/id_rsa", ".env", ".env.local"])
def test_the_other_protected_paths_are_untouched_by_the_exception(path, project):
    assert engine(root=project, allow=["read"]).evaluate("read", path) == Decision.ask
    assert engine(Mode.dontask, root=project, allow=["read"]).evaluate("read", path) == (
        Decision.deny
    )


@pytest.mark.parametrize("path", [
    ".quickcode/artifacts/../settings.json",
    ".quickcode/artifacts/../../../elsewhere/secret.md",
    ".quickcode/artifacts/../../.env",
])
def test_a_path_that_only_starts_inside_the_artifacts_directory_still_asks(path, project):
    """The prefix is not the test; the resolved location is."""
    assert engine(root=project, allow=["read"]).evaluate("read", path) == Decision.ask


def test_a_symlink_out_of_the_artifacts_directory_still_asks(project, tmp_path):
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    link = project / ".quickcode" / "artifacts" / "escape.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    assert engine(root=project).evaluate("read", str(link)) == Decision.ask


def test_an_artifact_read_falls_through_to_the_rules_rather_than_being_allowed(project):
    """Skipping the protected-path prompt is not an allow: a deny rule that
    covers the file still denies it, and an ask rule still asks."""
    assert engine(root=project, deny=["read(**.md)"]).evaluate("read", ARTIFACT) == Decision.deny
    assert engine(root=project, ask=["read(**.md)"]).evaluate("read", ARTIFACT) == Decision.ask


def test_a_shell_read_of_an_artifact_is_not_covered_by_the_exception(project):
    """``bash`` declares itself mutating, and its own protected-path scan is
    unchanged -- the exception is for tools that declare ``mutates=False``."""
    assert engine(root=project).evaluate("bash", f"cat {ARTIFACT}") == Decision.ask


def test_an_artifacts_directory_of_another_project_is_still_protected(project, tmp_path):
    other = tmp_path / "other" / ".quickcode" / "artifacts" / "explore-1.md"
    other.parent.mkdir(parents=True)
    assert engine(root=project).evaluate("read", str(other)) == Decision.ask


def test_yolo_does_not_ask_about_protected_paths_because_that_is_what_yolo_means(project):
    """The mode's whole promise is that it does not interrupt.

    It used to prompt anyway for anything protected — and for `bash`, where
    every non-option token is treated as a possible path, that meant a plain
    `find / -name "*x*"` stopped and waited on the `/`. Turning yolo on,
    confirming it, and watching the pill go red is the conversation; having it
    again per command is not a safety boundary, it is a mode that lies about
    what it is. Deny rules and the catastrophic-command breakers still stand.
    """
    e = engine(Mode.yolo, root=project)
    assert e.evaluate("read", ".env") == Decision.allow
    assert e.evaluate("read", ".git/config") == Decision.allow
    assert e.evaluate("bash", 'find / -name "*nimocam*" -type f') == Decision.allow
    assert e.evaluate("bash", "ls /etc") == Decision.allow
    # Still stopped: an explicit deny rule, and the patterns nobody means.
    assert engine(Mode.yolo, root=project, deny=["read(.env)"]).evaluate("read", ".env") == (
        Decision.deny
    )
    assert e.evaluate("bash", "rm -rf /") == Decision.ask
    assert e.evaluate("bash", ":(){ :|:& };:") == Decision.ask
