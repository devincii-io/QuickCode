"""The order the engine asks its questions in: deny, plan, protected, rules.

Three findings, each a case of an earlier question answering "ask" for a call a
later question would have refused:

1. The protected-path prompt came before the deny rules, so `read(**.env)`
   never denied `.env` -- the user was prompted, with an Allow button, for the
   one file they had said no to outright. Same for `bash`.
2. The protected-path prompt came before plan mode's refusal, so a write to
   `.git/config` in plan mode was a prompt rather than a refusal.
3. `auto-edit` allowed every mutating tool by default, not only edits: a
   `web_fetch`, a plugin's command tool, an MCP tool that writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import (
    Decision,
    Mode,
    PermissionEngine,
    PermissionSpec,
    Rules,
)
from quickcode.tools.web_fetch import WebFetchTool
from quickcode.tools.write import WriteTool

ALL_MODES = [Mode.plan, Mode.ask, Mode.auto_edit, Mode.dontask, Mode.yolo]


def engine(mode: Mode = Mode.ask, root: Path | None = None, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root or Path.cwd())


@pytest.mark.parametrize("mode", ALL_MODES)
def test_a_deny_rule_on_a_protected_path_denies_rather_than_asks(mode, tmp_path):
    e = engine(mode, root=tmp_path, deny=["read(**.env)", "bash(cat **)"])
    assert e.evaluate("read", ".env") == Decision.deny
    assert e.evaluate("read", str(tmp_path / ".env")) == Decision.deny
    assert e.evaluate("bash", "cat .env") == Decision.deny


@pytest.mark.parametrize("mode", ALL_MODES)
def test_a_deny_rule_on_a_path_outside_the_project_denies(mode, tmp_path):
    outside = str(tmp_path.parent / "elsewhere" / "secret.txt")
    e = engine(mode, root=tmp_path, deny=["read(**secret.txt)"])
    assert e.evaluate("read", outside) == Decision.deny


def test_without_the_deny_the_protected_path_still_asks(tmp_path):
    assert engine(root=tmp_path, allow=["read"]).evaluate("read", ".env") == Decision.ask


def test_plan_mode_refuses_a_protected_write_instead_of_prompting(tmp_path):
    e = engine(Mode.plan, root=tmp_path)
    assert e.evaluate("write", ".git/config") == Decision.deny
    assert e.evaluate("edit", ".env") == Decision.deny
    assert e.evaluate("bash", "rm .git/index") == Decision.deny
    assert e.evaluate("bash", "echo x > .git/hooks/pre-commit") == Decision.deny


def test_plan_mode_still_prompts_for_a_protected_read(tmp_path):
    e = engine(Mode.plan, root=tmp_path)
    assert e.evaluate("read", ".env") == Decision.ask
    assert e.evaluate("bash", "cat .env") == Decision.ask


def test_auto_edit_allows_edits_inside_the_project(tmp_path):
    e = engine(Mode.auto_edit, root=tmp_path)
    assert e.evaluate_tool(WriteTool(), {"file_path": "src/a.py", "content": ""})[0] == (
        Decision.allow
    )
    assert e.evaluate("edit", "src/a.py") == Decision.allow


def test_auto_edit_does_not_auto_allow_a_fetch(tmp_path):
    """A fetch is a way out for anything the agent has read."""
    e = engine(Mode.auto_edit, root=tmp_path)
    assert e.evaluate_tool(WebFetchTool(), {"url": "https://example.com/?q=x"})[0] == (
        Decision.ask
    )
    assert e.evaluate("web_search", "anything") == Decision.ask


def test_auto_edit_does_not_auto_allow_a_mutating_tool_without_a_path():
    """A plugin's command tool and an MCP tool that writes declare
    `mutates=True` and no path; neither is an edit."""
    e = engine(Mode.auto_edit)
    for spec in (PermissionSpec(mutates=True), PermissionSpec(mutates=True, target_field="q")):
        assert e.evaluate("mcp__issues__create", "x", spec=spec) == Decision.ask


def test_an_allow_rule_still_lets_a_mutating_tool_run_in_auto_edit():
    e = engine(Mode.auto_edit, allow=["web_fetch(https://docs.example.com/**)"])
    assert e.evaluate("web_fetch", "https://docs.example.com/a") == Decision.allow
    assert e.evaluate("web_fetch", "https://other.example.com/a") == Decision.ask
