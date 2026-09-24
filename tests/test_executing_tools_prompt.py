"""Tools that run a program are not edits, and ``auto-edit`` must say so.

docs/PERMISSIONS.md: "auto-edit auto-allows *edits*, and nothing else" -- a
shell command still prompts. An authored command tool is a narrowed shell
command and an MCP tool is a call into another process, yet both declared only
``mutates=True``, which the engine's ``auto-edit`` default reads as "an edit"
and allows. So switching to auto-edit silently ran every project command tool
and every MCP tool without a prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.kernel.authoring import discovery
from quickcode.plugins.mcp import MCPServer, MCPToolAdapter
from tests.test_authoring import ECHO, echo_tool, write


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


def _engine(mode: Mode, root: Path, **rules) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root)


def _command_tool(project: Path):
    write(project, "echo-args", echo_tool([*ECHO, "{path}"], [{"name": "path", "type": "path"}],
                                          permission_target="path"))
    return discovery.discover(project).plugins[0].to_tool()


def _mcp_tool(read_only: bool = False):
    server = MCPServer("docs", "unused", [], {})
    spec = {"name": "search", "inputSchema": {"type": "object"}}
    if read_only:
        spec["annotations"] = {"readOnlyHint": True}
    return MCPToolAdapter(server, spec)


def test_auto_edit_prompts_for_a_command_tool(project):
    tool = _command_tool(project)
    decision, _ = _engine(Mode.auto_edit, project).evaluate_tool(tool, {"path": "src/a.py"})
    assert decision == Decision.ask


def test_auto_edit_prompts_for_a_mutating_mcp_tool(tmp_path):
    decision, _ = _engine(Mode.auto_edit, tmp_path).evaluate_tool(_mcp_tool(), {})
    assert decision == Decision.ask


def test_an_allow_rule_still_lets_them_run_without_asking(project):
    tool = _command_tool(project)
    e = _engine(Mode.auto_edit, project, allow=["echo-args", "mcp__docs__search"])
    assert e.evaluate_tool(tool, {"path": "src/a.py"})[0] == Decision.allow
    assert e.evaluate_tool(_mcp_tool(), {})[0] == Decision.allow


def test_edits_and_read_only_tools_keep_their_auto_edit_behaviour(project):
    e = _engine(Mode.auto_edit, project)
    assert e.evaluate("write", "src/new.py") == Decision.allow
    assert e.evaluate_tool(_mcp_tool(read_only=True), {})[0] == Decision.allow


def test_yolo_still_runs_them(project):
    e = _engine(Mode.yolo, project)
    assert e.evaluate_tool(_command_tool(project), {"path": "src/a.py"})[0] == Decision.allow
    assert e.evaluate_tool(_mcp_tool(), {})[0] == Decision.allow
