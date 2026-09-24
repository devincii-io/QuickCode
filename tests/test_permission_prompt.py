"""What the permission prompt carries, end to end: the exact rules "Always
allow" will save (and saves), the diff an edit would make, and "Why?" asked of
the gate that raised the prompt -- through the real server and a real loop."""

from __future__ import annotations

import asyncio
import json

from quickcode.core.agent import GatedCall, PermissionRequest
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.registry import default_registry
from tests.test_server import FakeProvider, make_client, make_manager, recv_until, ws_connect


def _call(cid: str, name: str, **args) -> ToolCallEnd:
    return ToolCallEnd(id=cid, name=name, arguments=json.dumps(args))


def _open(client) -> str:
    return client.post("/api/conversations", json={}).json()["conv_id"]


def _start(ws, text: str = "go") -> None:
    ws.receive_json()
    recv_until(ws, "replay_done")
    ws.send_text(json.dumps({"type": "user_message", "text": text}))


def _answer(ws, req: dict, *, persist: bool) -> dict:
    ws.send_text(json.dumps({"type": "permission_decision", "req_id": req["req_id"],
                             "allow": True, "persist": persist}))
    return recv_until(ws, "permission_resolved")


def test_always_allow_saves_exactly_the_rules_the_prompt_showed(tmp_path):
    provider = FakeProvider([
        [_call("c1", "bash", command="FOO=1 true && echo done"), TurnDone("tool_calls")],
        [TextDelta("ok"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = _open(client)
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            _start(ws)
            req = recv_until(ws, "permission_request")
            # `echo done` is a read-only builtin and needs no rule; the other
            # half is saved as written, never as `bash(FOO=1 *)`.
            assert req["rules"] == ["bash(FOO=1 true)"]
            assert req["rule_suggestion"] == "bash(FOO=1 true)"
            assert req["kept"] == [] and req["diff"] == "" and req["hook_reason"] == ""
            resolved = _answer(ws, req, persist=True)
            assert resolved["saved"] == ["bash(FOO=1 true)"]
            recv_until(ws, "assistant_message")
        live = manager.get(conv_id).agent.permissions

    saved = json.loads((tmp_path / ".quickcode" / "settings.local.json").read_text("utf-8"))
    assert saved["permissions"]["allow"] == ["bash(FOO=1 true)"]
    assert live.evaluate("bash", "FOO=1 true").value == "allow"
    assert live.evaluate("bash", "FOO=1 rm -rf build").value == "ask"


def test_a_prompt_with_nothing_to_save_says_so_and_saves_nothing(tmp_path):
    provider = FakeProvider([
        [_call("c1", "bash", command="cat .env"), TurnDone("tool_calls")],
        [TextDelta("ok"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = _open(client)
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            _start(ws)
            req = recv_until(ws, "permission_request")
            assert req["rules"] == []
            assert req["kept"] == [{"part": "cat .env", "reason": "protected_path"}]
            # A client that sends persist anyway gets an allow-once.
            assert _answer(ws, req, persist=True)["saved"] == []
            recv_until(ws, "assistant_message")
    assert not (tmp_path / ".quickcode" / "settings.local.json").exists()


def test_an_edit_prompt_carries_the_diff_it_would_make(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    provider = FakeProvider([
        [_call("c1", "read", file_path=str(target)), TurnDone("tool_calls")],
        [_call("c2", "edit", file_path=str(target), old_string="b = 2", new_string="b = 20"),
         TurnDone("tool_calls")],
        [TextDelta("ok"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = _open(client)
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            _start(ws)
            req = recv_until(ws, "permission_request")
            assert req["tool"] == "edit"
            lines = req["diff"].splitlines()
            assert "-b = 2" in lines and "+b = 20" in lines
            assert " a = 1" in lines, "the file's own context, since the session read it"
            assert req["rules"] == [f"edit({target})"]

            why = client.post("/api/permissions/explain",
                              json={"conv": conv_id, "review": req["req_id"]})
            assert why.status_code == 200, why.text
            assert why.json()["decision"] == "ask"
            assert why.json()["target"] == str(target)
            assert why.json()["suggestion"]["rules"] == req["rules"]
            _answer(ws, req, persist=False)
            recv_until(ws, "assistant_message")
    assert target.read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"


async def test_why_asks_the_gate_of_the_agent_that_asked(tmp_path):
    """A subagent's engine is capped and carries none of the session's allow
    rules, so the conversation's own gate would give the wrong answer."""
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    conv.agent.permissions.rules.allow.append("bash(make)")
    bash = default_registry().get("bash")
    child = PermissionEngine(mode=Mode.ask, rules=Rules(), root=tmp_path)
    req = PermissionRequest(
        tool="bash", arg="make", rule_suggestion="bash(make)", agent_name="worker-1",
        rules=["bash(make)"], gated=GatedCall(bash, {"command": "make"}, child),
    )
    pending = asyncio.create_task(conv.permission_cb(req))
    await asyncio.sleep(0)
    [review] = conv.pending

    from quickcode.server.permissions_api import explain_payload

    payload = explain_payload(manager, {"conv": conv.conv_id, "review": review})
    assert payload["decision"] == "ask"
    assert payload["decided_by"]["step"] == "mode_default"
    assert payload["offered"] is True
    session = explain_payload(manager, {"conv": conv.conv_id, "command": "make"})
    assert session["decision"] == "allow"

    conv.resolve_permission(review, allow=False, persist=False, deny_message="")
    await pending


def test_why_needs_a_prompt_that_is_still_waiting(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = _open(client)
        gone = client.post("/api/permissions/explain", json={"conv": conv_id, "review": "nope"})
        assert gone.status_code == 404
        mixed = client.post("/api/permissions/explain",
                            json={"conv": conv_id, "review": "nope", "mode": "yolo"})
        assert mixed.status_code == 400
        assert client.post("/api/permissions/explain",
                           json={"review": "nope"}).status_code == 404


async def test_the_request_on_the_wire_never_carries_the_gate(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    bash = default_registry().get("bash")
    engine = PermissionEngine(mode=Mode.ask, rules=Rules(), root=tmp_path)
    req = PermissionRequest(tool="bash", arg="make", rule_suggestion="",
                            gated=GatedCall(bash, {"command": "make"}, engine))

    task = asyncio.create_task(conv.permission_cb(req))
    await asyncio.sleep(0)
    [entry] = conv.state_event()["pending"]
    conv.resolve_permission(entry["req_id"], allow=False, persist=False, deny_message="")
    await task

    assert "gated" not in entry
    [logged] = [e for e in conv.store.load_events() if e["type"] == "permission_request"]
    assert "gated" not in logged
    assert set(logged) >= {"rules", "kept", "diff", "hook_reason"}
