"""End-to-end server tests: REST bootstrap, WS streaming, permission
round-trip, and event-log replay — with a scripted fake provider."""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone, Usage
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager


class FakeProvider:
    """Yields one scripted response per request, in order."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    async def stream_chat(self, req):
        self.requests.append(req)
        script = self.scripts.pop(0) if self.scripts else [TextDelta("(done)"), TurnDone("stop")]
        for ev in script:
            yield ev

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def make_env(cwd: Path) -> Environment:
    return Environment(
        cwd=str(cwd), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )


def make_manager(tmp_path: Path, provider) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    return ConversationManager(
        cwd=tmp_path, config=cfg, env=make_env(tmp_path), provider=provider,
    )


def make_client(manager) -> TestClient:
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    # TestClient sends Host: testserver by default; use an allowed host.
    return TestClient(app, base_url="http://127.0.0.1:8642")


def ws_connect(client, path):
    # TestClient's WS handshake carries Host: testserver; the local guard wants
    # the loopback host it was configured with.
    return client.websocket_connect(path, headers={"host": "127.0.0.1:8642"})


def recv_until(ws, type_, limit=200):
    """Read events until one of ``type_`` arrives (fails after ``limit``)."""
    wanted = {type_} if isinstance(type_, str) else set(type_)
    for _ in range(limit):
        ev = ws.receive_json()
        if ev.get("type") in wanted:
            return ev
    raise AssertionError(f"never received {type_}")


def test_bootstrap_and_health(tmp_path):
    provider = FakeProvider([])
    with make_client(make_manager(tmp_path, provider)) as client:
        assert client.get("/api/health").json()["app"] == "quickcode"
        bs = client.get("/api/bootstrap").json()
        assert bs["cwd"] == str(tmp_path)
        assert bs["default_model"] == "test/model"


def test_bad_host_rejected(tmp_path):
    provider = FakeProvider([])
    app = create_app(make_manager(tmp_path, provider), host="127.0.0.1", port=8642, token="")
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/api/health").status_code == 403


def test_token_required_when_set(tmp_path):
    provider = FakeProvider([])
    app = create_app(make_manager(tmp_path, provider), host="127.0.0.1", port=8642, token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        assert client.get("/api/bootstrap").status_code == 403
        ok = client.get("/api/bootstrap", headers={"x-quickcode-token": "s3cret"})
        assert ok.status_code == 200
        # health stays open as the liveness probe
        assert client.get("/api/health").status_code == 200


def test_chat_turn_streams_and_logs(tmp_path):
    provider = FakeProvider([
        [TextDelta("Hello "), TextDelta("world"), Usage(input_tokens=10, output_tokens=5),
         TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            state = ws.receive_json()
            assert state["type"] == "state"
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "hi"}))
            assert recv_until(ws, "user_message")["text"] == "hi"
            msg = recv_until(ws, "assistant_message")
            assert msg["text"] == "Hello world"
            assert msg["seq"] > 0

        # The event log replays the identical transcript on reconnect.
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()  # state
            recv_until(ws, "replay_start")
            types = []
            while True:
                ev = ws.receive_json()
                if ev["type"] == "replay_done":
                    break
                types.append(ev["type"])
            assert "system_prompt" in types
            assert "user_message" in types
            assert "assistant_message" in types


def test_permission_roundtrip_allow(tmp_path):
    write_call = ToolCallEnd(
        id="c1", name="write",
        arguments=json.dumps({"file_path": str(tmp_path / "out.txt"), "content": "ok"}),
    )
    provider = FakeProvider([
        [write_call, TurnDone("tool_calls")],
        [TextDelta("written"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "write it"}))
            req = recv_until(ws, "permission_request")
            assert req["tool"] == "write"
            ws.send_text(json.dumps(
                {"type": "permission_decision", "req_id": req["req_id"],
                 "allow": True, "persist": False}))
            resolved = recv_until(ws, "permission_resolved")
            assert resolved["allow"] is True
            result = recv_until(ws, "tool_result")
            assert result["is_error"] is False
            recv_until(ws, "assistant_message")
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "ok"


def test_permission_roundtrip_deny(tmp_path):
    write_call = ToolCallEnd(
        id="c1", name="write",
        arguments=json.dumps({"file_path": str(tmp_path / "no.txt"), "content": "x"}),
    )
    provider = FakeProvider([
        [write_call, TurnDone("tool_calls")],
        [TextDelta("understood"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "write it"}))
            req = recv_until(ws, "permission_request")
            ws.send_text(json.dumps(
                {"type": "permission_decision", "req_id": req["req_id"],
                 "allow": False, "persist": False, "deny_message": "not today"}))
            result = recv_until(ws, "tool_result")
            assert result["is_error"] is True
            assert "not today" in result["content"]
    assert not (tmp_path / "no.txt").exists()


def test_mode_switch_and_state(tmp_path):
    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "set_mode", "mode": "auto-edit"}))
            assert recv_until(ws, "mode_changed")["mode"] == "auto-edit"
            # yolo is refused without --yolo
            ws.send_text(json.dumps({"type": "set_mode", "mode": "yolo"}))
            assert "yolo" in recv_until(ws, "error")["message"]


def test_plugins_endpoint(tmp_path):
    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        inv = client.get("/api/plugins").json()
        names = {t["name"] for t in inv["tools"]}
        assert {"read", "write", "edit", "bash"} <= names
        assert all(t["source"] == "builtin" for t in inv["tools"])
