"""The conversation server when clients are slow, vanish, or ask for two
things at once."""

from __future__ import annotations

import asyncio

from quickcode.core.compact import COMPACTION_PROMPT
from quickcode.core.events import TextDelta, TurnDone
from quickcode.providers.base import ModelInfo
from quickcode.server import manager as manager_module
from quickcode.session.store import SessionStore
from tests.test_background_agents import _settle
from tests.test_server import (
    FakeProvider,
    make_client,
    make_manager,
    recv_until,
    ws_connect,
)

# ---- a client that falls behind ----


def _drain(client) -> list[str | None]:
    out = []
    while not client.queue.empty():
        out.append(client.queue.get_nowait())
    return out


def test_a_client_that_falls_behind_is_told_to_resync_instead_of_skipping_events(monkeypatch):
    """The queue is bounded so a slow reader cannot hold the agent up; the
    promise that makes dropping safe is the sentinel, which closes the socket
    so the client reconnects and replays. It could never be enqueued -- the
    queue it was meant for was, by definition, full -- so the client went on
    receiving events after a silent gap."""
    monkeypatch.setattr(manager_module, "CLIENT_QUEUE_MAX", 3)
    client = manager_module.Client()
    for i in range(10):
        client.send(f"ev{i}")

    delivered = _drain(client)
    assert delivered[-1] is None, "the resync sentinel never arrived"
    assert "ev9" not in delivered, "an event after the gap was delivered as if nothing was lost"
    assert client.overflowed


def test_a_client_that_keeps_up_gets_every_event_and_no_sentinel(monkeypatch):
    monkeypatch.setattr(manager_module, "CLIENT_QUEUE_MAX", 3)
    client = manager_module.Client()
    seen = []
    for i in range(10):
        client.send(f"ev{i}")
        seen += _drain(client)
    assert seen == [f"ev{i}" for i in range(10)]


# ---- /compact and a message sent while it runs ----


class GatedCompactionProvider:
    """Answers turns at once; a compaction request waits on ``gate``."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.compacting = asyncio.Event()
        self.log: list[str] = []

    async def stream_chat(self, req):
        last = req.messages[-1]
        if last.role == "user" and last.content == COMPACTION_PROMPT:
            self.log.append("compaction")
            self.compacting.set()
            await self.gate.wait()
            yield TextDelta("the summary")
            yield TurnDone("stop")
            return
        self.log.append("turn")
        yield TextDelta("an answer")
        yield TurnDone("stop")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


async def test_a_message_sent_during_a_manual_compaction_waits_for_it(tmp_path):
    """Compaction rebuilds the history wholesale. A turn that starts while
    the summary is still being written runs against the history it is about
    to lose, and the two send requests on one conversation at once."""
    provider = GatedCompactionProvider()
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("first")
        await _settle(conv)

        conv.request_compact()
        await asyncio.wait_for(provider.compacting.wait(), 5)
        conv.submit("second")
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert provider.log == ["turn", "compaction"], "a turn ran inside the compaction"

        provider.gate.set()
        for _ in range(300):
            await asyncio.sleep(0.01)
            if provider.log[-1] == "turn" and not conv.agent.busy and conv._inbox.empty():
                break
        await _settle(conv)
        assert provider.log == ["turn", "compaction", "turn"]
    finally:
        await manager.close()


async def test_closing_a_conversation_stops_a_compaction_still_in_flight(tmp_path):
    """Otherwise it outlives the conversation and writes its record into a
    session that was just deleted, which brings the file back."""
    provider = GatedCompactionProvider()
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    conv.submit("first")
    await _settle(conv)
    conv.request_compact()
    await asyncio.wait_for(provider.compacting.wait(), 5)

    await manager.close()
    provider.gate.set()
    for _ in range(10):
        await asyncio.sleep(0.01)
    kinds = [r.get("kind") for r in SessionStore(tmp_path, conv.conv_id)._iter_records()]
    assert "compaction" not in kinds


# ---- frames the protocol does not use ----


def test_a_binary_or_malformed_frame_is_dropped_and_the_socket_keeps_working(tmp_path):
    """The protocol is JSON text frames. Anything else is dropped, the way a
    frame that is not JSON already was, instead of ending the socket with a
    server-side traceback."""
    provider = FakeProvider([[TextDelta("still here"), TurnDone("stop")]])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            ws.send_bytes(b"\x00\xffnot text")
            ws.send_bytes(b'{"type": "user_message", "text": "ignored as bytes"}')
            ws.send_text("[1, 2, 3]")
            ws.send_text("{not json")
            ws.send_json({"type": "user_message", "text": "hello"})
            assert recv_until(ws, "assistant_message")["text"] == "still here"
        assert len(provider.requests) == 1


# ---- deleting a selection ----


def test_a_bulk_delete_closes_an_idle_open_conversation_the_way_a_single_delete_does(tmp_path):
    """Opened earlier in this run and since left alone is not "live": the
    single delete route closes such a session and deletes it, and the bulk
    route reported the same session as live and kept it."""
    provider = FakeProvider([[TextDelta("hi"), TurnDone("stop")]])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            ws.send_json({"type": "user_message", "text": "hello"})
            recv_until(ws, "assistant_message")
        # The socket is gone and the turn is over: open, but idle.
        assert conv_id in manager.conversations

        body = client.post("/api/sessions/delete", json={"conv_ids": [conv_id]}).json()
        assert body["deleted"] == [conv_id]
        assert body["skipped"] == []
        assert conv_id not in manager.conversations
        assert not SessionStore(tmp_path, conv_id).path.exists()
