"""``quickcode -p`` runs with the tools the app would give the same project.

The app adds entry-point plugin tools and the project's MCP servers to every
session's pool; ``-p`` started neither, so a composition granting
``mcp__docs__*`` -- or a prompt that needed a plugin tool -- behaved
differently headless than in the window. These drive the real CLI with a
scripted provider and ``tests/mcp_stub.py`` as a real MCP server, and assert
what the model was offered, what a call returned, what stderr said, and that
nothing was left running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from quickcode import cli
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.plugins import loader, mcp_turn
from quickcode.security import trust
from quickcode.tools.base import PermissionSpec, Tool, ToolResult
from tests.conftest import await_until, wait_until
from tests.test_headless import _events, _headless, _install
from tests.test_server import FakeProvider

STUB = str(Path(__file__).with_name("mcp_stub.py"))
READ_ONLY_ECHO = json.dumps([{"name": "echo", "annotations": {"readOnlyHint": True}}])


def _servers(project: Path, **servers: dict) -> None:
    path = project / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _stub(**env: str) -> dict:
    return {"command": sys.executable, "args": [STUB], "env": env}


def _call_then_answer(name: str, args: dict | None = None) -> list:
    return [
        [ToolCallEnd(id="c1", name=name, arguments=json.dumps(args or {})),
         TurnDone("tool_calls")],
        [TextDelta("done"), TurnDone("stop")],
    ]


def _offered(provider) -> set[str]:
    return {t.name for t in provider.requests[0].tools}


def _result(tmp_path: Path) -> str:
    return next(e for e in _events(tmp_path) if e["type"] == "tool_result")["content"]


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie nobody reaped is still gone
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


# ---- entry-point plugin tools ----------------------------------------------


class _NoInput(BaseModel):
    pass


class _LintRepo(Tool[_NoInput]):
    Input = _NoInput
    name = "lint_repo"
    description = "Lints the repository."
    is_read_only = True
    permission = PermissionSpec(mutates=False)

    async def run(self, input, ctx):  # noqa: A002
        return ToolResult(content="0 problems")


class _EP:
    name = "lint"

    @staticmethod
    def load():
        return lambda: [_LintRepo()]


def test_a_headless_run_offers_and_runs_an_entry_point_plugin_tool(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(loader, "entry_points",
                        lambda group: [_EP()] if group == loader.TOOLS_GROUP else [])
    provider = FakeProvider(_call_then_answer("lint_repo"))
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "lint it"))

    assert capsys.readouterr().out.strip() == "done"
    assert "lint_repo" in _offered(provider)
    assert _result(tmp_path) == "0 problems"


# ---- MCP servers -------------------------------------------------------------


def test_a_trusted_projects_mcp_server_serves_the_headless_turn(tmp_path, monkeypatch, capsys):
    _servers(tmp_path, stub=_stub(STUB_TOOLS=READ_ONLY_ECHO))
    trust.default_store().grant(tmp_path)
    provider = FakeProvider(_call_then_answer("mcp__stub__echo", {"x": 1}))
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "echo"))

    out, err = capsys.readouterr()
    assert out.strip() == "done"
    assert "mcp__stub__echo" in _offered(provider)
    echo = json.loads(_result(tmp_path))
    assert echo["tool"] == "echo" and echo["args"] == {"x": 1}
    # It ran in the project, as the app runs a project's server.
    assert Path(echo["cwd"]).resolve() == tmp_path.resolve()
    assert "MCP" not in err


def test_an_untrusted_projects_mcp_server_does_not_start_and_the_run_says_so(
        tmp_path, monkeypatch, capsys):
    marker = tmp_path / "STARTED"
    _servers(tmp_path, stub={"command": sys.executable,
                             "args": ["-c", f"open({str(marker)!r}, 'w').close()"]})
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "hello"))

    _out, err = capsys.readouterr()
    assert not marker.exists(), "an untrusted project's MCP command must not run"
    assert not any(n.startswith("mcp__") for n in _offered(provider))
    assert "not trusted" in err and "stub" in err


def test_a_user_mcp_server_starts_without_project_trust(tmp_path, monkeypatch, capsys):
    """The app's gate, not a stricter one: the user's own servers always start."""
    import quickcode.config as config_module

    home = config_module.CONFIG_DIR
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"mcpServers": {"mine": _stub()}}), encoding="utf-8")
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "hello"))

    assert "mcp__mine__echo" in _offered(provider)


def test_no_mcp_leaves_the_servers_unstarted(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "STARTED"
    _servers(tmp_path, stub={"command": sys.executable,
                             "args": ["-c", f"open({str(marker)!r}, 'w').close()"]})
    trust.default_store().grant(tmp_path)
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "--no-mcp", "hello"))

    assert capsys.readouterr().out.strip() == "ok"
    assert not marker.exists()


def test_a_server_that_never_answers_costs_the_timeout_and_no_more(
        tmp_path, monkeypatch, capsys):
    """A hung server must not hang a script. The run waits the stated time,
    says which server it gave up on, and goes on without it."""
    _servers(tmp_path,
             silent={"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]},
             stub=_stub())
    trust.default_store().grant(tmp_path)
    monkeypatch.setattr(mcp_turn, "START_TIMEOUT_S", 1.0)
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    started = time.monotonic()
    cli.main(_headless(tmp_path, "hello"))
    elapsed = time.monotonic() - started

    out, err = capsys.readouterr()
    assert out.strip() == "ok"
    assert elapsed < 20
    assert "MCP server silent did not start within 1s" in err
    # The server that did come up is still in the run.
    offered = _offered(provider)
    assert "mcp__stub__echo" in offered
    assert not any(n.startswith("mcp__silent__") for n in offered)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_the_servers_a_headless_run_started_are_gone_when_it_exits(
        tmp_path, monkeypatch, capsys):
    pidfile = tmp_path / "pids"
    _servers(tmp_path, stub=_stub(STUB_PIDFILE=str(pidfile)))
    trust.default_store().grant(tmp_path)
    _install(monkeypatch, FakeProvider([[TextDelta("ok"), TurnDone("stop")]]))

    cli.main(_headless(tmp_path, "hello"))

    leader, child = (int(x) for x in pidfile.read_text().split())
    assert wait_until(lambda: _gone(leader) and _gone(child))


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_an_interrupt_during_start_up_leaves_nothing_running(tmp_path):
    """Ctrl+C lands while a server is still coming up: the start is cancelled
    before it handed anything back, and ``stop`` still ends that server."""
    _servers(tmp_path,
             silent={"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]})
    trust.default_store().grant(tmp_path)
    servers = mcp_turn.plan(tmp_path)
    [server] = servers.servers

    async def go():
        start = asyncio.create_task(servers.start(timeout=30))
        await await_until(lambda: server.proc is not None)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        await servers.stop()
        return server.proc.pid

    pid = asyncio.run(go())
    assert servers.servers == []
    assert wait_until(lambda: _gone(pid))
