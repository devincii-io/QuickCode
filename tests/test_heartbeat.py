"""An idle socket still says something, so silence means the socket is dead.

After a laptop sleeps and wakes, a WebSocket can sit in OPEN with nothing alive
behind it: sends succeed into the void, and on a conversation nobody is driving,
no frame ever arrives to prove otherwise. The client cannot tell that apart from
"the agent is thinking" — there is nothing to miss. So the server sends a beat
on a quiet socket, and the client (frontend/js/ws.js) closes one that has missed
several in a row and reconnects the ordinary way.
"""

from __future__ import annotations

from pathlib import Path

from quickcode.server import app as app_module
from tests.test_server import FakeProvider, make_client, make_manager, recv_until, ws_connect


def test_a_quiet_socket_is_sent_a_heartbeat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "HEARTBEAT_S", 0.05)
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            # Nothing is happening on this conversation, so the beat is the
            # only thing that can arrive — and it has to.
            assert recv_until(ws, "heartbeat")["type"] == "heartbeat"


def test_a_heartbeat_is_never_part_of_the_transcript(tmp_path: Path, monkeypatch) -> None:
    """The session log is what replays as a transcript, and a keep-alive is not
    something that happened in the conversation."""
    monkeypatch.setattr(app_module, "HEARTBEAT_S", 0.05)
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            beat = recv_until(ws, "heartbeat")
        assert "seq" not in beat, "a heartbeat carried a sequence number"
        conv = manager.get(conv_id)
        assert not any(
            ev.get("type") == "heartbeat" for ev in conv.store.replay_events()
        ), "a heartbeat reached the session log"
