"""``TranscriptRecorder.record_turn`` driven more than once on one agent.

``-p`` runs one turn per process, but nothing about the recorder says so: a
second call on the same recorder and agent is a second turn of the same
session and must log like one.
"""

from __future__ import annotations

from quickcode.core.events import TextDelta, TurnDone
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore
from tests.test_server import FakeProvider
from tests.test_subagent_usage import _agent


async def test_a_second_recorded_turn_logs_each_event_once(tmp_path):
    provider = FakeProvider([
        [TextDelta("first"), TurnDone("stop")],
        [TextDelta("second"), TurnDone("stop")],
    ])
    agent = _agent(provider)
    store = SessionStore(tmp_path)
    rec = TranscriptRecorder(store)

    await rec.record_turn(agent, "one")
    await rec.record_turn(agent, "two")

    said = [(e["turn"], e["text"]) for e in store.load_events()
            if e["type"] == "assistant_message"]
    assert said == [(1, "first"), (2, "second")]
    # The bus is left as the turn found it: nothing of a finished turn's
    # stays subscribed to hear the next one.
    assert agent.bus._subs == []
