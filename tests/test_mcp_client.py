"""The MCP stdio client against a server that misbehaves on purpose.

``tests/mcp_stub.py`` is a real process speaking real JSON-RPC; each test asks
it for one kind of trouble through its environment, the way a user's
settings.json would configure it. What is asserted is always the user-visible
outcome: the call answers, answers fast, or fails with a reason -- never a
120-second hang, a wrong result, or a process left behind.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from quickcode.plugins import mcp, mcp_wire
from quickcode.security.trust import TrustStore
from tests.conftest import await_until

STUB = str(Path(__file__).with_name("mcp_stub.py"))


def _server(name: str = "stub", **env: str) -> mcp.MCPServer:
    return mcp.MCPServer(name, sys.executable, [STUB], dict(env))


def _run(coro):
    return asyncio.run(coro)


async def _call(server: mcp.MCPServer, tool: str = "echo", **args):
    return await server.call_tool(tool, args)


# ---- the transport -------------------------------------------------------


def test_a_message_over_64_kib_is_read_whole():
    """asyncio's default line limit is 64 KiB. Past it the reader died, and
    this call -- and every later one -- hung until its timeout."""
    async def go():
        server = _server(STUB_BIG=str(300_000))
        await server.start()
        try:
            text, is_error = await asyncio.wait_for(_call(server), 20)
            assert not is_error
            assert len(text) == 300_000
        finally:
            await server.stop()

    _run(go())


def test_a_large_tools_list_is_read_whole():
    async def go():
        server = _server(STUB_MANY_TOOLS="400")
        await server.start()
        try:
            assert len(server.tools) == 400
        finally:
            await server.stop()

    _run(go())


def test_a_server_that_floods_stderr_still_starts():
    """A megabyte of stderr before the handshake: undrained, the pipe fills
    and the server blocks mid-write, never answering initialize."""
    async def go():
        server = _server(STUB_STDERR=str(1_000_000))
        await asyncio.wait_for(server.start(), 20)
        try:
            assert (await _call(server))[1] is False
        finally:
            await server.stop()

    _run(go())


def test_a_server_request_that_reuses_our_id_is_answered_not_taken_as_the_reply():
    """Both sides number requests from 1. The server's ``ping`` with id 3 used
    to be read as the reply to *our* request 3 -- the call returned {} -- and
    the ping itself was never answered."""
    async def go():
        server = _server(STUB_PING="1")
        await server.start()
        try:
            text, is_error = await asyncio.wait_for(_call(server, value=1), 20)
            assert not is_error
            assert json.loads(text)["args"] == {"value": 1}
        finally:
            await server.stop()

    _run(go())


def test_a_crash_fails_the_call_at_once_and_the_next_call_restarts(tmp_path):
    marker = tmp_path / "crashed-once"

    async def go():
        server = _server(STUB_CRASH=str(marker))
        await server.start()
        try:
            started = time.monotonic()
            with pytest.raises(mcp.MCPError, match="exited"):
                await _call(server)
            assert time.monotonic() - started < 10
            first_pid = server.proc.pid
            text, is_error = await asyncio.wait_for(_call(server), 20)
            assert not is_error
            assert json.loads(text)["pid"] != first_pid
        finally:
            await server.stop()

    _run(go())


def test_a_server_that_keeps_dying_is_not_restarted_forever(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_RESTARTS", 2)

    async def go():
        server = _server(STUB_CRASH="1")
        await server.start()
        try:
            for _ in range(3):
                with pytest.raises(mcp.MCPError):
                    await asyncio.wait_for(_call(server), 20)
            with pytest.raises(mcp.MCPError, match="not again"):
                await _call(server)
        finally:
            await server.stop()

    _run(go())


def test_after_stop_a_call_fails_at_once_and_nothing_restarts():
    async def go():
        server = _server()
        await server.start()
        await server.stop()
        started = time.monotonic()
        with pytest.raises(mcp.MCPError, match="stopped"):
            await _call(server)
        assert time.monotonic() - started < 2
        assert server.proc.returncode is not None

    _run(go())


def test_a_call_that_never_answers_times_out_with_a_reason(monkeypatch):
    monkeypatch.setattr(mcp, "CALL_TIMEOUT_S", 0.5)

    async def go():
        server = _server(STUB_HANG="1")
        await server.start()
        try:
            tool = mcp.adapters(server)[0]
            result = await tool.run(tool.Input(), None)
            assert result.is_error
            assert "no answer within" in result.content
        finally:
            await server.stop()

    _run(go())


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_stop_ends_the_server_and_what_it_started(tmp_path):
    """``npx server`` is npx, then node. Terminating the direct child left the
    server itself running."""
    pidfile = tmp_path / "pids"

    def gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # a zombie nobody reaped is still gone
            return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except OSError:
            return True

    async def go():
        server = _server(STUB_PIDFILE=str(pidfile))
        await server.start()
        leader, child = (int(x) for x in pidfile.read_text().split())
        assert not gone(child)
        await server.stop()
        await await_until(lambda: gone(leader) and gone(child))

    _run(go())


# ---- what the session says about the server ------------------------------


def test_the_negotiated_protocol_version_is_recorded():
    async def go():
        server = _server(STUB_VERSION="2024-11-05")
        await server.start()
        try:
            assert server.protocol_version == "2024-11-05"
        finally:
            await server.stop()

    _run(go())


def test_a_start_failure_says_why_without_printing_the_servers_secret():
    async def go():
        server = mcp.MCPServer("stub", sys.executable, [STUB],
                               {"API_KEY": "sk-live-abcdef123456", "STUB_LEAK": "API_KEY"})
        with pytest.raises(mcp.MCPError) as info:
            await server.start()
        await server.stop()
        message = str(info.value)
        assert "auth failed" in message
        assert "sk-live-abcdef123456" not in message

    _run(go())


def test_a_project_server_runs_in_its_project(tmp_path, monkeypatch):
    monkeypatch.setattr("quickcode.config.CONFIG_DIR", tmp_path / "home")
    project = tmp_path / "proj"
    (project / ".quickcode").mkdir(parents=True)
    (project / ".quickcode" / "settings.json").write_text(json.dumps(
        {"mcpServers": {"stub": {"command": sys.executable, "args": [STUB]}}}),
        encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)

    async def go():
        servers, tools = await mcp.connect_servers(project, store=store)
        try:
            text = (await tools[0].run(tools[0].Input(), None)).content
            assert Path(json.loads(text)["cwd"]).resolve() == project.resolve()
        finally:
            for s in servers:
                await s.stop()

    _run(go())


def test_a_trusted_project_server_shadows_a_user_server_of_the_same_name(tmp_path, monkeypatch):
    """Two processes answering to one name produced two tools with one name,
    which the provider rejects outright."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("quickcode.config.CONFIG_DIR", home)
    spec = {"command": sys.executable, "args": [STUB]}
    (home / "settings.json").write_text(json.dumps({"mcpServers": {"stub": spec}}),
                                        encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".quickcode").mkdir(parents=True)
    (project / ".quickcode" / "settings.json").write_text(
        json.dumps({"mcpServers": {"stub": spec}}), encoding="utf-8")
    store = TrustStore(tmp_path / "trust.json")
    store.grant(project)

    async def go():
        servers, tools = await mcp.connect_servers(project, store=store)
        try:
            assert [(s.name, s.scope) for s in servers] == [("stub", "project")]
            assert [t.name for t in tools] == ["mcp__stub__echo"]
        finally:
            for s in servers:
                await s.stop()

    _run(go())


# ---- names and results (pure) --------------------------------------------


def test_a_name_that_fits_is_used_as_written():
    assert mcp_wire.tool_name("docs", "search_all") == "mcp__docs__search_all"


@pytest.mark.parametrize("server,tool", [
    ("docs", "files.read"), ("my server", "search"), ("docs", "a/b"),
    ("docs", "x" * 80), ("gh", "créer"),
])
def test_a_name_a_provider_would_refuse_is_made_to_fit_and_stays_stable(server, tool):
    name = mcp_wire.tool_name(server, tool)
    assert len(name) <= 64
    assert all(c.isascii() and (c.isalnum() or c in "_-") for c in name)
    assert name.startswith("mcp__")
    assert name == mcp_wire.tool_name(server, tool)


def test_two_names_that_clean_to_the_same_text_stay_apart():
    assert mcp_wire.tool_name("docs", "a.b") != mcp_wire.tool_name("docs", "a/b")
    assert mcp_wire.tool_name("docs", "a.b") != mcp_wire.tool_name("docs", "a_b")


def test_an_adapter_uses_the_provider_safe_name_and_an_object_schema():
    server = mcp.MCPServer("my.server", "unused", [], {})
    adapter = mcp.MCPToolAdapter(server, {"name": "files.read",
                                          "inputSchema": {"properties": {}}})
    schema = adapter.schema()
    assert schema.name == mcp_wire.tool_name("my.server", "files.read")
    assert schema.parameters["type"] == "object"


def test_rich_content_is_described_rather_than_dropped():
    text, is_error = mcp_wire.render_result({"content": [
        {"type": "text", "text": "hello"},
        {"type": "resource", "resource": {"uri": "file:///a.txt", "text": "body"}},
        {"type": "resource_link", "uri": "file:///b.txt", "name": "b"},
        {"type": "image", "mimeType": "image/png", "data": "AAAA"},
    ]})
    assert not is_error
    assert "hello" in text
    assert "[resource file:///a.txt]\nbody" in text
    assert "file:///b.txt" in text
    assert "image/png" in text and "AAAA" not in text


def test_structured_content_is_shown_when_there_is_no_text():
    text, _ = mcp_wire.render_result({"content": [], "structuredContent": {"n": 1}})
    assert json.loads(text) == {"n": 1}
