"""Subagent spend: counted as money, not as context.

Every subagent is its own ``AgentInstance`` with its own ``Ledger``, so a
fan-out of ten workers used to cost the session exactly nothing — the child's
usage events were live-only, dropped after rendering, and absent from the log a
resume rebuilds from. These tests pin the fix and, just as importantly, its
boundary: the cumulative totals take a child's tokens, and ``last_input_tokens``
never does, because that pair *is* the context meter and a subagent runs in a
context window of its own.

The last two cover the other half of ``record_turn``: a headless ``-p`` run now
checks the same compaction threshold the web worker does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from quickcode.core.agent import AgentInstance, EventBus, Ledger, PermissionOutcome
from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel.composition import RuntimeLimits
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore
from tests.test_server import FakeProvider, make_manager

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class StubRegistry:
    tools: dict = {}

    def schemas(self):
        return []

    def get(self, name):
        return None


async def _deny(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=False)


def _agent(provider, *, context_length: int | None = None,
           limits: RuntimeLimits | None = None) -> AgentInstance:
    return AgentInstance(
        name="main",
        provider=provider,
        registry=StubRegistry(),
        history=History("SYS"),
        ctx=None,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="test/model",
        permission_cb=_deny,
        context_length=context_length,
        limits=limits,
    )


async def _child_spends(rec: TranscriptRecorder, agent_id: str, usage: Usage) -> None:
    """One subagent's bus, bridged the way a real spawn bridges it."""
    bus = EventBus()
    rec.on_subagent(agent_id, agent_id.rsplit("-", 1)[0], bus)
    bus.emit(usage)
    bus.emit(TurnDone("stop"))
    rec.drain()
    for task in rec.child_pumps.values():
        task.cancel()
    await asyncio.gather(*rec.child_pumps.values(), return_exceptions=True)


# --------------------------------------------------------------------------
# the ledger split
# --------------------------------------------------------------------------


async def test_a_subagents_tokens_reach_the_session_ledger_but_not_the_context_meter(
    tmp_path,
):
    agent = _agent(FakeProvider([]), context_length=1000)
    agent.ledger.add(Usage(input_tokens=100, output_tokens=10, cost_usd=0.01))
    before_pct = agent.context_pct()

    rec = TranscriptRecorder(SessionStore(tmp_path), ledger=agent.ledger)
    await _child_spends(
        rec, "explore-1", Usage(input_tokens=5000, output_tokens=400, cost_usd=0.5)
    )

    # Spend: the session paid for the child, so the session counts it.
    assert agent.ledger.input_tokens == 5100
    assert agent.ledger.output_tokens == 410
    assert agent.ledger.cost_usd == 0.51
    assert agent.ledger.subagent_input_tokens == 5000
    assert agent.ledger.subagent_cost_usd == 0.5
    # Context: the child's 5000-token request went into its own window. Folding
    # it in here would show a 10%-full conversation as over its threshold.
    assert agent.ledger.last_input_tokens == 100
    assert agent.ledger.last_output_tokens == 10
    assert agent.context_pct() == before_pct


async def test_a_subagents_usage_survives_into_the_log_and_replays_the_same_way(
    tmp_path,
):
    store = SessionStore(tmp_path)
    live = Ledger()
    rec = TranscriptRecorder(store, ledger=live)
    rec.emit({"type": "user_message", "text": "fan out"})
    live.add(Usage(input_tokens=100, output_tokens=10, cost_usd=0.01))
    rec.emit({"type": "usage", "input_tokens": 100, "output_tokens": 10, "cost_usd": 0.01})
    await _child_spends(
        rec, "explore-1", Usage(input_tokens=5000, output_tokens=400, cost_usd=0.5)
    )

    events = store.load_events()
    child_usage = [
        e for e in events
        if e["type"] == "agent_event" and e["ev"]["type"] == "usage"
    ]
    assert len(child_usage) == 1
    assert child_usage[0]["agent_id"] == "explore-1"
    # The spend belongs to the turn that spawned it, so the by-turn view can
    # attribute it without guessing.
    assert child_usage[0]["turn"] == 1

    # A reopened session rebuilds the identical split from the log alone.
    rebuilt = Ledger.from_events(events)
    assert rebuilt.input_tokens == live.input_tokens == 5100
    assert rebuilt.cost_usd == live.cost_usd
    assert rebuilt.subagent_input_tokens == 5000
    assert rebuilt.subagent_cost_usd == 0.5
    assert rebuilt.last_input_tokens == 100


async def test_a_live_conversation_reports_the_subagent_share_on_its_state_event(
    tmp_path,
):
    """The wiring that makes the status bar and the Usage panel agree: the
    conversation hands its own ledger to the recorder, so a child's usage lands
    in the object ``state_event`` reads."""
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    conv.agent.context_length = 10_000
    try:
        await _child_spends(
            conv.rec, "explore-1", Usage(input_tokens=2000, output_tokens=50, cost_usd=0.2)
        )
        ledger = conv.state_event()["ledger"]
        assert ledger["input_tokens"] == 2000
        assert ledger["subagent_input_tokens"] == 2000
        assert ledger["subagent_cost_usd"] == 0.2
        # The context meter is untouched: nothing was added to this agent's
        # history, so nothing may claim its window is filling.
        assert conv.state_event()["context_pct"] == 0.0
    finally:
        await conv.close()


async def test_compaction_forgets_the_context_footprint_and_not_what_was_spent():
    """Rebuilding history does not un-spend the tokens that built it — and the
    meter must drop, or the threshold that triggered the compaction stays
    tripped until the next request happens to re-measure."""
    from quickcode.core.compact import run_compaction, should_compact

    provider = FakeProvider([[TextDelta("SUMMARY: earlier work"), TurnDone("stop")]])
    agent = _agent(provider, context_length=100)
    agent.ledger.add(Usage(input_tokens=90, output_tokens=5, cost_usd=0.3))
    agent.ledger.add_subagent(Usage(input_tokens=4000, output_tokens=100, cost_usd=0.4))
    assert should_compact(agent) is True

    await run_compaction(agent, keep_turns=1)

    assert should_compact(agent) is False
    assert agent.ledger.input_tokens == 4090
    assert agent.ledger.cost_usd == 0.7
    assert agent.ledger.subagent_cost_usd == 0.4


# --------------------------------------------------------------------------
# headless compaction
# --------------------------------------------------------------------------


def _script(input_tokens: int) -> list:
    return [
        TextDelta("done"),
        Usage(input_tokens=input_tokens, output_tokens=5),
        TurnDone("stop"),
    ]


async def test_a_headless_turn_over_the_threshold_compacts_like_the_web_path_does(
    tmp_path,
):
    provider = FakeProvider([_script(90), [TextDelta("SUMMARY: earlier work"), TurnDone("stop")]])
    agent = _agent(provider, context_length=100, limits=RuntimeLimits(keep_turns=1))
    store = SessionStore(tmp_path)
    rec = TranscriptRecorder(store)

    await rec.record_turn(agent, "go")

    kinds = [e["type"] for e in store.load_events()]
    assert "compacted" in kinds
    assert agent.history.messages[0].role == "user"
    assert "compaction-summary" in agent.history.messages[0].content
    # The rebuilt history is already accounted for; without this fix-up the
    # summary seed would be appended to the log again on the next turn.
    assert rec.persisted == len(agent.history.messages)
    # And the turn's own messages reached disk before the rebuild replaced them.
    assert any("go" in m.content for m in store.load_messages())


async def test_a_headless_turn_under_the_threshold_is_left_alone(tmp_path):
    provider = FakeProvider([_script(10)])
    agent = _agent(provider, context_length=100)
    rec = TranscriptRecorder(SessionStore(tmp_path))

    await rec.record_turn(agent, "go")

    assert agent.take_post_compaction() is False
    assert len(provider.requests) == 1  # no summarization request was made


async def test_headless_compaction_obeys_the_setting_that_switches_it_off(tmp_path):
    provider = FakeProvider([_script(90)])
    agent = _agent(
        provider, context_length=100, limits=RuntimeLimits(compaction_enabled=False)
    )
    rec = TranscriptRecorder(SessionStore(tmp_path))

    await rec.record_turn(agent, "go")

    assert len(provider.requests) == 1
    assert agent.take_post_compaction() is False


async def test_a_headless_run_rolls_its_subagents_into_the_ledger_it_adopts(tmp_path):
    """The CLI builds its recorder before the agent exists, so the ledger is
    adopted at ``record_turn``; without that a ``-p`` fan-out reports nothing."""
    provider = FakeProvider([_script(10)])
    agent = _agent(provider)
    rec = TranscriptRecorder(SessionStore(tmp_path))
    assert rec.ledger is None

    await rec.record_turn(agent, "go")
    assert rec.ledger is agent.ledger

    await _child_spends(rec, "explore-1", Usage(input_tokens=700, output_tokens=20))
    assert agent.ledger.subagent_input_tokens == 700
    assert agent.ledger.input_tokens == 710
    assert agent.ledger.last_input_tokens == 10


def test_a_childs_usage_never_becomes_the_parents_last_request():
    """``add`` and ``add_subagent`` are separate paths on purpose: only the
    first may move the footprint, and a later parent request must overwrite it
    rather than accumulate a child's."""
    ledger = Ledger()
    ledger.add_subagent(Usage(input_tokens=10, output_tokens=1, cached_tokens=2))
    assert (ledger.last_input_tokens, ledger.last_output_tokens) == (0, 0)
    ledger.add(Usage(input_tokens=3, output_tokens=4))
    assert (ledger.last_input_tokens, ledger.last_output_tokens) == (3, 4)
    assert ledger.input_tokens == 13
    assert ledger.cached_tokens == 2
