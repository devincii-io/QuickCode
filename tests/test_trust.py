"""The project trust gate (quickcode/security/trust.py).

Covers the security-critical invariants: an untrusted project's MCP servers do
not spawn, a trusted one's do, editing the config re-prompts, a project cannot
declare itself trusted from inside its own tree, and revocation works.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from quickcode.plugins import mcp
from quickcode.security import trust
from quickcode.security.trust import TrustStore


def _write_settings(project: Path, servers: dict) -> None:
    qd = project / ".quickcode"
    qd.mkdir(parents=True, exist_ok=True)
    (qd / "settings.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _marker_server(marker: Path) -> dict:
    """An 'MCP server' that only writes a marker file, proving it was spawned.

    It is not a real MCP server, so the JSON-RPC handshake fails and the server
    is dropped — but the OS command has already run by then, which is exactly
    the RCE. The marker is the ground truth for 'did this execute'.
    """
    return {
        "pwn": {
            "command": sys.executable,
            "args": ["-c", f"open(r'{marker}', 'w').write('spawned')"],
        }
    }


# ---- hashing / status ----


def test_no_servers_is_not_trusted_but_has_nothing_to_gate(tmp_path):
    store = TrustStore(tmp_path / "trust.json")
    status = store.status(tmp_path)
    assert status.has_servers is False
    assert status.inert is False
    assert status.trusted is False


def test_default_is_untrusted(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {"docs": {"command": "npx", "args": ["-y", "x"]}})
    store = TrustStore(tmp_path / "trust.json")
    status = store.status(project)
    assert status.has_servers is True
    assert status.trusted is False
    assert status.inert is True
    assert status.server_names == ["docs"]


# ---- spawn gating ----


def test_untrusted_project_does_not_spawn(tmp_path):
    project = tmp_path / "proj"
    marker = project / "PWNED.txt"
    _write_settings(project, _marker_server(marker))
    store = TrustStore(tmp_path / "trust.json")

    async def go():
        servers, tools = await mcp.connect_servers(project, store=store)
        return servers, tools

    servers, tools = asyncio.run(go())
    assert marker.exists() is False, "untrusted project must not execute its MCP command"
    assert servers == []
    assert tools == []


def test_trusted_project_spawns(tmp_path):
    project = tmp_path / "proj"
    marker = project / "OK.txt"
    _write_settings(project, _marker_server(marker))
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    assert store.is_trusted(project) is True

    async def go():
        servers, _ = await mcp.connect_servers(project, store=store)
        # give the spawned interpreter a moment to write its marker
        await asyncio.sleep(2.0)
        for s in servers:
            await s.stop()

    asyncio.run(go())
    assert marker.exists() is True, "trusted project's MCP command should run"


# ---- re-prompt on edit ----


def test_editing_config_reprompts(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    assert store.is_trusted(project) is True

    # Add a second server: the hash changes, so the old grant no longer applies.
    _write_settings(project, {
        "a": {"command": "npx", "args": ["1"]},
        "b": {"command": "evil", "args": ["2"]},
    })
    assert store.is_trusted(project) is False
    assert store.status(project).inert is True

    # Reverting to the exact trusted config trusts again (hash matches).
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    assert store.is_trusted(project) is True


# ---- authored command tools gate under the same grant ----


def _write_plugin(project: Path, filename: str, kind: str, body: str = "") -> Path:
    pd = project / ".quickcode" / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    path = pd / filename
    path.write_text(f"---\nkind: {kind}\ntitle: t\n---\n{body}\n", encoding="utf-8")
    return path


def test_adding_a_command_tool_reprompts(tmp_path):
    """The gap this closes: a trusted project could add an executable tool.

    Trust was bound to mcpServers alone, so a project already approved for its
    servers could commit a new command tool and have it run with no prompt —
    the MCP hole again, through a different file.
    """
    project = tmp_path / "proj"
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    assert store.is_trusted(project) is True

    _write_plugin(project, "deploy.md", "tool", "```json argv\n[\"sh\"]\n```")
    assert store.is_trusted(project) is False, "a new command tool must re-prompt"
    assert store.status(project).inert is True


def test_editing_a_command_tool_reprompts(tmp_path):
    project = tmp_path / "proj"
    path = _write_plugin(project, "build.md", "tool", "```json argv\n[\"make\"]\n```")
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    assert store.is_trusted(project) is True

    # Same filename, same kind, different program.
    path.write_text(path.read_text(encoding="utf-8").replace("make", "curl"),
                    encoding="utf-8")
    assert store.is_trusted(project) is False


def test_tools_alone_are_gated_and_reported(tmp_path):
    """A project with no servers but a command tool still has something to gate."""
    project = tmp_path / "proj"
    _write_plugin(project, "run.md", "tool")
    store = TrustStore(tmp_path / "trust.json")
    status = store.status(project)
    assert status.has_servers is False
    assert status.has_tools is True
    assert status.tool_files == ["run.md"]
    assert status.inert is True, "an untrusted command tool is a refusal, not a no-op"
    assert "command tools" in status.reason
    assert status.to_json()["tools"] == ["run.md"]


def test_authored_text_does_not_affect_trust(tmp_path):
    """Agents and prompt sections are text, and text is not gated.

    Re-prompting when someone edits a prompt section would train the reflex
    that makes the prompt worthless for the case that matters.
    """
    project = tmp_path / "proj"
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)

    _write_plugin(project, "reviewer.md", "agent", "You review code.")
    _write_plugin(project, "tone.md", "prompt", "Be terse.")
    assert store.is_trusted(project) is True
    assert store.status(project).tool_files == []


def test_unreadable_kind_is_gated(tmp_path):
    """A declaration we cannot parse is assumed to be the dangerous one."""
    project = tmp_path / "proj"
    pd = project / ".quickcode" / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "mystery.md").write_text("no frontmatter at all\n", encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")
    assert store.status(project).tool_files == ["mystery.md"]


def test_trash_is_not_scanned(tmp_path):
    project = tmp_path / "proj"
    trash = project / ".quickcode" / "plugins" / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    (trash / "old.md").write_text("---\nkind: tool\n---\n", encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")
    assert store.status(project).tool_files == []


# ---- cannot self-declare trust ----


def test_project_cannot_declare_itself_trusted(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    # An attacker plants a trust.json *inside* the repo claiming trust.
    inside = project / ".quickcode" / "trust.json"
    inside.write_text(json.dumps({
        "version": 1,
        "projects": {str(project).replace("\\", "/").lower(): {
            "hash": trust.config_hash(project),
        }},
    }), encoding="utf-8")

    # The real store lives at user scope and never reads the project tree.
    store = TrustStore(tmp_path / "trust.json")
    assert store.is_trusted(project) is False


# ---- revocation ----


def test_revoke(tmp_path):
    project = tmp_path / "proj"
    _write_settings(project, {"a": {"command": "npx", "args": ["1"]}})
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    assert store.is_trusted(project) is True

    assert store.revoke(project) is True
    assert store.is_trusted(project) is False
    # Revoking again is a harmless no-op.
    assert store.revoke(project) is False


def test_revoked_project_does_not_spawn(tmp_path):
    project = tmp_path / "proj"
    marker = project / "PWNED.txt"
    _write_settings(project, _marker_server(marker))
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)
    store.revoke(project)

    async def go():
        servers, _ = await mcp.connect_servers(project, store=store)
        await asyncio.sleep(1.0)
        for s in servers:
            await s.stop()

    asyncio.run(go())
    assert marker.exists() is False


# ---- user-scope servers are never gated ----


def test_user_scope_servers_bypass_gate(tmp_path, monkeypatch):
    # Point the user config dir at a temp location with its own settings.json.
    user_dir = tmp_path / "userconfig"
    user_dir.mkdir()
    marker = tmp_path / "USER_OK.txt"
    (user_dir / "settings.json").write_text(
        json.dumps({"mcpServers": {
            "mine": {"command": sys.executable,
                     "args": ["-c", f"open(r'{marker}', 'w').write('user')"]}
        }}), encoding="utf-8")
    monkeypatch.setattr("quickcode.config.CONFIG_DIR", user_dir)

    project = tmp_path / "proj"  # untrusted, no project servers
    project.mkdir()
    store = TrustStore(tmp_path / "trust.json")

    async def go():
        servers, _ = await mcp.connect_servers(project, store=store)
        await asyncio.sleep(2.0)
        for s in servers:
            await s.stop()

    asyncio.run(go())
    assert marker.exists() is True, "user's own MCP servers must run without a trust prompt"


# ---- HTTP endpoints (report / grant / revoke) over an injected store ----


def _make_trust_app(tmp_path: Path, project: Path):
    from starlette.testclient import TestClient

    from quickcode.config import Config
    from quickcode.server.app import create_app
    from quickcode.server.projects import ProjectHub, ProjectRegistry
    from tests.test_server import FakeProvider

    cfg = Config()
    cfg.last_model = "test/model"
    hub = ProjectHub(
        config=cfg,
        provider=FakeProvider([]),
        registry=ProjectRegistry(tmp_path / "projects.json"),
        mcp_connect=mcp.connect_servers,  # the real gate
        trust_store=TrustStore(tmp_path / "trust.json"),
    )
    asyncio.run(hub.open(project, make_default=True))
    app = create_app(hub, host="127.0.0.1", port=8642, token="")
    return hub, TestClient(app, base_url="http://127.0.0.1:8642")


def test_trust_endpoints_report_grant_revoke(tmp_path):
    project = tmp_path / "proj"
    # A command that fails to spawn fast (no such binary): grant records trust
    # and the connect attempt returns nothing, without a lingering process.
    _write_settings(project, {"docs": {"command": "qc-no-such-binary-xyz", "args": []}})
    hub, client = _make_trust_app(tmp_path, project)
    with client:
        status = client.get("/api/trust").json()
        assert status["trusted"] is False
        assert status["inert"] is True
        assert status["servers"] == ["docs"]

        granted = client.post("/api/trust").json()
        assert granted["trusted"] is True
        assert granted["inert"] is False

        assert client.get("/api/trust").json()["trusted"] is True

        revoked = client.delete("/api/trust").json()
        assert revoked["revoked"] is True
        assert revoked["trusted"] is False
        assert client.get("/api/trust").json()["inert"] is True


def test_trust_report_shows_what_a_command_tool_would_run(tmp_path):
    """Consent needs the command, not the filename.

    An untrusted tool is dropped from the registry, so the report has to read
    the file itself — otherwise the banner asks the user to approve 'deploy.md'
    and the whole point of showing commands is lost.
    """
    project = tmp_path / "proj"
    pd = project / ".quickcode" / "plugins"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "deploy.md").write_text(
        "---\nkind: tool\nname: deploy\ntitle: Deploy\n"
        "description: Push the current branch\n---\n"
        "```json params\n[]\n```\n"
        '```json argv\n["git", "push", "--force"]\n```\n',
        encoding="utf-8")

    hub, client = _make_trust_app(tmp_path, project)
    with client:
        status = client.get("/api/trust").json()
        assert status["inert"] is True
        assert status["tools"] == ["deploy.md"]
        detail = {t["name"]: t for t in status["tool_detail"]}
        assert detail["deploy"]["argv"] == ["git", "push", "--force"]


def test_trust_endpoint_unknown_project_404(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    hub, client = _make_trust_app(tmp_path, project)
    with client:
        assert client.get("/api/projects/deadbeef/trust").status_code == 404
