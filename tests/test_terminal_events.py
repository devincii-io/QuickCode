"""Every ending the server knows about is announced on the event stream.

The bug these pin is one bug wearing several hats: the server knew a thing had
ended and did not say so, and every reader downstream was left inferring it.
A tool call whose result never arrives is a spinner nobody stops. A turn cut
off mid-stream that never flips to ``interrupted`` leaves its half-sentence in
the accumulator, to be glued onto the *next* turn's message. A round whose
usage is logged but never counted makes the same session cost two different
amounts depending on who adds it up. And a turn parked on a permission modal
that nothing will ever answer stays ``busy`` forever.

Everything here drives a real ``Conversation`` against a provider the test
decides when to answer, and reads the claim back out of the session log --
which is what a reconnecting or replaying client reads too.
"""

from __future__ import annotations

import asyncio
import json

from quickcode.core.agent import Ledger
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone, Usage
from quickcode.providers.base import ModelInfo
from tests.test_background_agents import _settle
from tests.test_server import make_manager


class StallingProvider:
    """Says a few words, reports its usage, then streams nothing forever.

    The stall is a generator that keeps yielding, because that is what a real
    stream does: the loop only notices a cancel between events, so a provider
    that simply blocks would hang the turn instead of ending it.
    """

    def __init__(self) -> None:
        self.talking = asyncio.Event()

    async def stream_chat(self, _req):
        yield TextDelta("half a thought")
        yield Usage(input_tokens=120, output_tokens=7, cost_usd=0.5)
        self.talking.set()
        while True:
            await asyncio.sleep(0.01)
            yield TextDelta("")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


class GatedChildProvider:
    """Main agent runs a scripted round; the child waits on a gate."""

    def __init__(self, main: list) -> None:
        self.main = list(main)
        self.gate = asyncio.Event()
        self.child_started = asyncio.Event()

    async def stream_chat(self, req):
        system = next((m.content for m in req.messages if m.role == "system"), "")
        if "QuickCode subagent" in (system or ""):
            self.child_started.set()
            await self.gate.wait()
            yield TextDelta("the child's report")
            yield TurnDone("stop")
            return
        script = self.main.pop(0) if self.main else [TextDelta("(done)"), TurnDone("stop")]
        for ev in script:
            yield ev

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def _spawn_round() -> list:
    args = json.dumps({
        "description": "dig the logs", "prompt": "dig", "agent_type": "explore",
    })
    return [ToolCallEnd(id="c1", name="agent", arguments=args), TurnDone("tool_calls")]


def _by_type(conv, type_: str) -> list[dict]:
    return [e for e in conv.store.load_events() if e.get("type") == type_]


async def test_every_logged_tool_call_gets_a_result_even_when_interrupted(tmp_path):
    provider = GatedChildProvider([_spawn_round()])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("dig through the logs")
        await provider.child_started.wait()
        conv.interrupt()
        await _settle(conv)

        calls = {e["id"] for e in _by_type(conv, "tool_call")}
        results = {e["id"] for e in _by_type(conv, "tool_result")}
        assert calls and calls == results
        assert _by_type(conv, "tool_result")[0]["content"] == "[interrupted]"
    finally:
        await manager.close()


async def test_a_subagent_cut_off_by_an_interrupt_announces_that_it_ended(tmp_path):
    provider = GatedChildProvider([_spawn_round()])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("dig through the logs")
        await provider.child_started.wait()
        conv.interrupt()
        await _settle(conv)

        spawned = _by_type(conv, "agent_spawned")
        done = _by_type(conv, "agent_done")
        assert [e["agent_id"] for e in spawned] == [e["agent_id"] for e in done]
        assert done[0]["status"] == "cancelled"
        assert done[0]["definition"] == "explore"
    finally:
        await manager.close()


async def test_a_blocking_subagent_announces_its_ending_in_the_log(tmp_path):
    provider = GatedChildProvider([_spawn_round()])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        provider.gate.set()
        conv.submit("dig through the logs")
        await _settle(conv)

        done = _by_type(conv, "agent_done")
        assert [e["status"] for e in done] == ["done"]
        # The closing bracket sits after everything the child emitted, so a
        # reader replaying the log in order never has to look ahead.
        log = conv.store.load_events()
        last_child_event = max(
            i for i, e in enumerate(log)
            if e.get("type") == "agent_event" and e["agent_id"] == done[0]["agent_id"]
        )
        assert log.index(done[0]) > last_child_event
    finally:
        await manager.close()


async def test_an_interrupt_mid_stream_closes_the_turn_it_cut_off(tmp_path):
    provider = StallingProvider()
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("think out loud")
        await provider.talking.wait()
        conv.interrupt()
        await _settle(conv)

        # The half-sentence belongs to the turn it was said in, tagged as cut
        # off -- not left in the accumulator to reappear inside the next one.
        messages = _by_type(conv, "assistant_message")
        assert [m["finish_reason"] for m in messages] == ["interrupted"]
        assert messages[0]["text"] == "half a thought"
        assert any(e["text"] == "(interrupted)" for e in _by_type(conv, "system_note"))
    finally:
        await manager.close()


async def test_the_ledger_counts_an_interrupted_round_the_way_a_replay_does(tmp_path):
    provider = StallingProvider()
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("think out loud")
        await provider.talking.wait()
        conv.interrupt()
        await _settle(conv)

        live = conv.agent.ledger
        replayed = Ledger.from_events(conv.store.load_events())
        assert live.input_tokens == replayed.input_tokens == 120
        assert live.cost_usd == replayed.cost_usd == 0.5
    finally:
        await manager.close()


async def test_interrupting_a_turn_parked_on_a_permission_prompt_ends_it(tmp_path):
    call = [ToolCallEnd(
        id="w1", name="write",
        arguments=json.dumps({"file_path": str(tmp_path / "x.txt"), "content": "hi"}),
    ), TurnDone("tool_calls")]
    provider = GatedChildProvider([call])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.submit("write the file")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if conv.pending:
                break
        assert conv.pending, "the write never asked for permission"

        conv.interrupt()
        await _settle(conv)

        # Nothing is running, so nothing may still look like it is.
        assert conv.agent.busy is False
        assert conv.pending == {}
        resolved = _by_type(conv, "permission_resolved")
        assert resolved and resolved[0]["allow"] is False
        assert not (tmp_path / "x.txt").exists()
    finally:
        await manager.close()
