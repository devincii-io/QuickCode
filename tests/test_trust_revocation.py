"""Revoking trust has to take effect, not just stop the *next* start.

Before this, revoking a project left its MCP servers running and their tools
in the list every *new* conversation was built from, and a command tool held by
an open conversation kept running its program. "Revoke" is the button someone
presses after reading a config and deciding they do not want it; a revocation
that changes nothing until the app restarts is not one.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from quickcode.kernel.authoring import discovery
from quickcode.plugins import mcp
from quickcode.security import trust
from quickcode.security.trust import TrustStore
from tests.test_authoring import ECHO, echo_tool, run_tool, write

STUB = str(Path(__file__).with_name("mcp_stub.py"))


def test_a_command_tool_stops_running_once_trust_is_revoked(tmp_path, monkeypatch):
    import quickcode.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "home")
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    write(project, "echo-args", echo_tool([*ECHO, "hi"], []))
    granted = {"now": True}
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: granted["now"])

    tool = discovery.command_tools(project)[0]  # what an open conversation holds
    assert json.loads(run_tool(tool, project).content.strip()) == ["hi"]

    granted["now"] = False
    result = run_tool(tool, project)
    assert result.is_error
    assert "not trusted" in result.content
    assert "argv" not in (result.ui_meta or {})


def test_a_user_scope_command_tool_is_not_gated(tmp_path, monkeypatch):
    import quickcode.config as config_module

    home = tmp_path / "home"
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: False)
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "echo-args.md").write_text(echo_tool([*ECHO, "hi"], []),
                                                   encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    tool = discovery.command_tools(project)[0]
    assert not run_tool(tool, project).is_error


def _hub(tmp_path: Path, project: Path, store: TrustStore):
    from quickcode.config import Config
    from quickcode.server.projects import ProjectHub, ProjectRegistry
    from tests.test_server import FakeProvider

    cfg = Config()
    cfg.last_model = "test/model"
    return ProjectHub(
        config=cfg, provider=FakeProvider([]),
        registry=ProjectRegistry(tmp_path / "projects.json"),
        mcp_connect=lambda cwd: mcp.connect_servers(cwd, store=store),
        trust_store=store,
    )


def test_revoking_stops_the_projects_servers_and_new_conversations_lose_them(
        tmp_path, monkeypatch):
    monkeypatch.setattr("quickcode.config.CONFIG_DIR", tmp_path / "home")
    project = tmp_path / "proj"
    (project / ".quickcode").mkdir(parents=True)
    (project / ".quickcode" / "settings.json").write_text(json.dumps(
        {"mcpServers": {"stub": {"command": sys.executable, "args": [STUB]}}}),
        encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)

    async def go():
        hub = _hub(tmp_path, project, store)
        manager = await hub.open(project, make_default=True)
        pid = hub.default_id
        try:
            assert manager.mcp_servers == ["stub"]
            held = manager.registry_factory().tools["mcp__stub__echo"]
            assert not (await held.run(held.Input(), None)).is_error

            report = await hub.revoke_trust(pid)
            assert report["revoked"] is True
            assert report["stopped"] == ["stub"]
            assert manager.mcp_servers == []
            assert "mcp__stub__echo" not in manager.registry_factory().tools
            # The conversation that already holds the tool gets an error, not
            # a server that outlived its trust.
            after = await held.run(held.Input(), None)
            assert after.is_error
            assert held._server.proc.returncode is not None
        finally:
            await hub.close()

    asyncio.run(go())
