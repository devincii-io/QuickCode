"""What the browser is allowed to assume when its socket comes back.

The web UI reconnects on every drop — a server restart, a laptop waking up, or
the server's own 1013 "you fell behind, reconnect and replay" close — and it
throws its whole mirror away first (``resetConversation`` in
frontend/js/store.js). Everything it puts back on screen therefore has to come
out of the attach, or it is gone. That the transcript replays in full is
covered in test_server.py; these are the other three promises the client now
depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from tests.test_server import FakeProvider, make_client, make_manager, recv_until, ws_connect


def _blocked_on_a_write(tmp_path: Path):
    """A conversation parked on a permission request, with its socket dropped.

    The turn is blocked inside the manager awaiting a decision future — the
    socket that started it is gone, which is exactly the case a reconnect has
    to be able to recover from.
    """
    write_call = ToolCallEnd(
        id="c1", name="write",
        arguments=json.dumps({"file_path": str(tmp_path / "out.txt"), "content": "ok"}),
    )
    provider = FakeProvider([
        [write_call, TurnDone("tool_calls")],
        [TextDelta("written"), TurnDone("stop")],
    ])
    return make_client(make_manager(tmp_path, provider))


def test_a_permission_request_left_unanswered_is_still_in_the_state_event_after_a_reconnect(
    tmp_path,
):
    with _blocked_on_a_write(tmp_path) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "write it"}))
            req_id = recv_until(ws, "permission_request")["req_id"]
            # Walk away without answering: the dialog dies with the socket, and
            # the only thing that can put it back is the next attach.

        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            state = ws.receive_json()
        assert state["type"] == "state"
        assert [p["req_id"] for p in state["pending"]] == [req_id]
        assert state["pending"][0]["tool"] == "write"


def test_a_permission_request_recovered_from_a_reconnect_can_still_be_answered(tmp_path):
    with _blocked_on_a_write(tmp_path) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "write it"}))
            recv_until(ws, "permission_request")

        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            state = ws.receive_json()
            recv_until(ws, "replay_done")
            req_id = state["pending"][0]["req_id"]
            ws.send_text(json.dumps(
                {"type": "permission_decision", "req_id": req_id,
                 "allow": True, "persist": False}))
            assert recv_until(ws, "tool_result")["is_error"] is False
            recv_until(ws, "assistant_message")
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "ok"


def test_a_turn_still_running_reports_itself_busy_to_a_socket_that_just_attached(tmp_path):
    """The Stop button is hidden the moment the socket drops — it cannot reach
    the agent, and offering to interrupt a turn you cannot interrupt is the
    kind of dead control that reads as "the app is stuck". Bringing it back is
    the replayed ``state`` event's job, so ``busy`` has to be true on it."""
    with _blocked_on_a_write(tmp_path) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            ws.receive_json()
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "write it"}))
            recv_until(ws, "permission_request")

        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            assert ws.receive_json()["busy"] is True


def test_attaching_to_a_conversation_the_server_does_not_have_closes_with_4404(tmp_path):
    """The one close the client must *not* retry.

    ``_attach`` accepts the socket and only then closes 4404, so a reconnect
    loop that treats every close alike reopens twice a second forever against
    an answer that will never change. js/ws.js reads this code and stops,
    offering a new session instead — which is only correct if the code is
    reliably this one.
    """
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        with pytest.raises(WebSocketDisconnect) as exc:  # noqa: PT012
            with ws_connect(client, "/ws/conversation/deadbeef0000") as ws:
                ws.receive_json()
        assert exc.value.code == 4404
