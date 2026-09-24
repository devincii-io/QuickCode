"""Resuming a session hands the provider a history it will accept.

The message log is appended turn by turn, and a turn can be cut anywhere: a
Ctrl-C in a headless run lands between the assistant's tool calls and their
results, a crash tears a line, an old build persisted in a different order.
OpenAI-compatible APIs reject any history where an assistant ``tool_calls``
message is not followed by one result per call, or where a tool result answers
a call nobody made -- and they reject it on *every* request, so one bad pair
bricks the conversation for good. Resume is where that gets repaired.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from quickcode import cli
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.providers.base import ChatMessage
from quickcode.session.store import SessionStore
from tests.test_headless import _headless, _install
from tests.test_server import FakeProvider, make_client, make_manager, recv_until, ws_connect


def _call(call_id: str, name: str = "read") -> dict:
    return {"id": call_id, "name": name, "arguments": "{}"}


def _store(tmp_path: Path, messages: list[ChatMessage]) -> SessionStore:
    store = SessionStore(tmp_path, "conv")
    for m in messages:
        store.append_message(m)
    return store


def _shape(messages: list[ChatMessage]) -> list[tuple[str, str | None]]:
    return [(m.role, m.tool_call_id) for m in messages]


def test_a_tool_call_left_without_a_result_gets_one_on_resume(tmp_path):
    store = _store(tmp_path, [
        ChatMessage(role="user", content="look at two files"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1"), _call("c2")]),
        ChatMessage(role="tool", content="one", tool_call_id="c1", name="read"),
    ])
    loaded = store.load_messages()
    assert _shape(loaded) == [
        ("user", None), ("assistant", None), ("tool", "c1"), ("tool", "c2"),
    ]
    filled = loaded[-1]
    assert filled.name == "read"
    assert filled.content.startswith("[error]")


def test_a_tool_result_whose_call_is_gone_is_dropped(tmp_path):
    # The assistant line that made the call was lost (a damaged line, or a
    # compaction boundary that kept the result but not the call).
    store = _store(tmp_path, [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="tool", content="orphan", tool_call_id="ghost", name="read"),
        ChatMessage(role="assistant", content="answer"),
    ])
    assert _shape(store.load_messages()) == [("user", None), ("assistant", None)]


def test_a_result_logged_twice_is_sent_once(tmp_path):
    store = _store(tmp_path, [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
        ChatMessage(role="tool", content="first", tool_call_id="c1", name="read"),
        ChatMessage(role="tool", content="again", tool_call_id="c1", name="read"),
        ChatMessage(role="assistant", content="done"),
    ])
    loaded = store.load_messages()
    assert _shape(loaded) == [
        ("user", None), ("assistant", None), ("tool", "c1"), ("assistant", None),
    ]
    assert loaded[2].content == "first"


def test_a_well_formed_history_is_returned_untouched(tmp_path):
    original = [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="calling", tool_calls=[_call("c1"), _call("c2")]),
        ChatMessage(role="tool", content="a", tool_call_id="c1", name="read"),
        ChatMessage(role="tool", content="b", tool_call_id="c2", name="read"),
        ChatMessage(role="assistant", content="done"),
    ]
    assert _store(tmp_path, original).load_messages() == original


def test_the_repair_is_the_same_on_every_resume(tmp_path):
    # Byte-stable across reopenings: the synthesized result is not persisted,
    # so it has to come out identical each time or the prompt cache breaks.
    store = _store(tmp_path, [
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=[_call("c1")]),
    ])
    assert store.load_messages() == SessionStore(tmp_path, "conv").load_messages()


class _CancelDuringTools(FakeProvider):
    """Ends round one with a tool call, then Ctrl-C lands while it runs."""

    async def stream_chat(self, req):
        if not self.requests:
            task = asyncio.current_task()
            asyncio.get_running_loop().call_soon(task.cancel)
        async for ev in super().stream_chat(req):
            yield ev


def test_a_headless_run_cut_off_mid_tool_can_be_continued(tmp_path, monkeypatch, capsys):
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    call = ToolCallEnd(id="c1", name="read", arguments=json.dumps({"file_path": str(target)}))
    _install(monkeypatch, _CancelDuringTools([[call, TurnDone("tool_calls")]]))
    with pytest.raises(asyncio.CancelledError):
        cli.main(_headless(tmp_path, "read it"))
    capsys.readouterr()

    second = FakeProvider([[TextDelta("carrying on"), TurnDone("stop")]])
    _install(monkeypatch, second)
    cli.main(_headless(tmp_path, "--continue", "go on"))
    assert capsys.readouterr().out.strip() == "carrying on"

    sent = second.requests[0].messages
    roles = [(m.role, m.tool_call_id) for m in sent]
    at = roles.index(("assistant", None), 1)
    assert sent[at].tool_calls and sent[at].tool_calls[0]["id"] == "c1"
    assert roles[at + 1] == ("tool", "c1")


# ---- turn numbering ----

def test_turns_keep_counting_after_a_resume(tmp_path):
    """The Usage panel groups spend by turn, so numbering that restarts at 1
    after a reopen folded the new turn's cost into the old turn 1."""
    provider = FakeProvider([
        [TextDelta("a"), TurnDone("stop")],
        [TextDelta("b"), TurnDone("stop")],
        [TextDelta("c"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            for text in ("one", "two"):
                ws.send_text(json.dumps({"type": "user_message", "text": text}))
                recv_until(ws, "assistant_message")

    reopened = make_manager(tmp_path, provider)
    with make_client(reopened) as client, ws_connect(
        client, f"/ws/conversation/{conv_id}"
    ) as ws:
        recv_until(ws, "replay_done")
        ws.send_text(json.dumps({"type": "user_message", "text": "three"}))
        recv_until(ws, "assistant_message")

    events = SessionStore(tmp_path, conv_id).load_events()
    turns = {e["text"]: e["turn"] for e in events if e["type"] == "user_message"}
    assert turns == {"one": 1, "two": 2, "three": 3}
    answer = [e for e in events if e["type"] == "assistant_message"][-1]
    assert answer["turn"] == 3
