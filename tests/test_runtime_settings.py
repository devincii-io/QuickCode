"""The six runtime knobs are the values the runtime actually uses.

Each of these settings was rendered by the Settings UI, accepted an edit and
saved it -- while the loop, the compactor and the spawner ran on a module
constant. These tests are about the wiring, not about the mechanisms the
wiring feeds: that compaction summarises correctly is ``test_compact.py``'s
question, and that a subagent is bounded is ``test_subagents.py``'s. Here the
question is only ever "does the declared number decide it".

Three properties, all of them load-bearing:

* the declared value takes effect,
* ``max_depth`` and ``max_agents`` stay backstops -- a settings file can lower
  them and cannot raise them past the maximum their own card declares,
* a running session keeps the limits it opened with.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel import manifest
from quickcode.kernel import state as state_store
from quickcode.kernel.composition import ORCHESTRATOR_ID, RuntimeLimits
from quickcode.kernel.preset import builtin_presets
from quickcode.kernel.resolve import resolve_composition, runtime_limits
from quickcode.subagents.runner import SubagentDeps, spawn_subagent
from quickcode.tools.registry import ToolRegistry
from tests.test_server import FakeProvider, make_manager

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class LoopingProvider:
    """Always asks for one tool call, so the loop only ends at its budget."""

    def __init__(self) -> None:
        self.requests = 0

    async def stream_chat(self, _req) -> AsyncIterator:
        self.requests += 1
        yield ToolCallEnd(id=f"c{self.requests}", name="nope", arguments="{}")
        yield Usage(input_tokens=1, output_tokens=1)
        yield TurnDone("tool_calls")

    async def list_models(self):
        return []


async def _deny(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=False)


def _agent(provider, limits: RuntimeLimits | None = None) -> AgentInstance:
    return AgentInstance(
        name="main",
        provider=provider,
        registry=ToolRegistry([]),
        history=History("SYS"),
        ctx=None,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="test/model",
        permission_cb=_deny,
        limits=limits,
    )


def _write(cwd: Path, plugin_id: str, **settings) -> None:
    state_store.save_entry(cwd, plugin_id, settings=settings)


async def _settle(conv, *, timeout: float = 5.0) -> None:
    """Wait until the conversation's worker is idle again."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        if not conv.agent.busy and conv._inbox.empty() and not conv.input_queue:
            return
    raise AssertionError("the conversation never went idle")


# --------------------------------------------------------------------------
# 1. runtime.agent_loop.max_rounds
# --------------------------------------------------------------------------

async def test_max_rounds_is_the_declared_setting():
    provider = LoopingProvider()
    await _agent(provider, RuntimeLimits(max_rounds=3)).run_turn("go")
    # One request per round, plus the wrap-up round the budget itself buys.
    assert provider.requests == 4

    other = LoopingProvider()
    await _agent(other, RuntimeLimits(max_rounds=1)).run_turn("go")
    assert other.requests == 2


async def test_max_rounds_defaults_to_the_manifest_value():
    assert runtime_limits(None).max_rounds == 50
    assert _agent(LoopingProvider()).limits.max_rounds == 50


def test_max_rounds_reads_the_project_settings_file(tmp_path):
    _write(tmp_path, "runtime.agent_loop", max_rounds=80)
    assert runtime_limits(tmp_path).max_rounds == 80


# --------------------------------------------------------------------------
# 2-4. runtime.compaction.{enabled,threshold,keep_turns}
# --------------------------------------------------------------------------

def _compaction_provider() -> FakeProvider:
    """One turn that fills 60% of the window, then a summary if asked for."""
    return FakeProvider([
        [TextDelta("worked"), Usage(input_tokens=60, output_tokens=0), TurnDone("stop")],
        [TextDelta("SUMMARY OF EVERYTHING"), TurnDone("stop")],
    ])


def _preload(conv) -> None:
    """Two older turns, so a keep_turns cut has something to cut."""
    from quickcode.core.events import AssistantMessage

    for i in range(2):
        conv.agent.history.push_user(f"OLD-MARKER-{i}")
        conv.agent.history.push_assistant(AssistantMessage(text=f"old answer {i}"))
    conv.rec.persisted = len(conv.agent.history.messages)


async def test_compaction_threshold_and_keep_turns_take_effect(tmp_path):
    # 0.6 of the window used; the default 0.8 would not fire and 0.5 does.
    _write(tmp_path, "runtime.compaction", threshold=0.5, keep_turns=1)
    manager = make_manager(tmp_path, _compaction_provider())
    conv = manager.open()
    try:
        conv.agent.context_length = 100
        _preload(conv)
        conv.submit("hi")
        await _settle(conv)

        messages = conv.agent.history.messages
        assert "compaction-summary" in messages[0].content
        assert "SUMMARY OF EVERYTHING" in messages[0].content
        # keep_turns=1: the seed plus the single most recent user turn.
        assert len(messages) == 3
        assert not any("OLD-MARKER" in m.content for m in messages)
    finally:
        await manager.close()


def test_keep_turns_zero_keeps_nothing_verbatim():
    """The declared minimum has to mean what it says: ``user_idxs[-0]`` is
    ``user_idxs[0]``, so 0 used to keep the entire transcript."""
    from quickcode.core.compact import _select_tail
    from quickcode.providers.base import ChatMessage

    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
    ]
    assert _select_tail(msgs, keep_turns=0) == []
    assert len(_select_tail(msgs, keep_turns=1)) == 1


async def test_compaction_enabled_false_leaves_the_transcript_alone(tmp_path):
    _write(tmp_path, "runtime.compaction", enabled=False, threshold=0.5)
    manager = make_manager(tmp_path, _compaction_provider())
    conv = manager.open()
    try:
        conv.agent.context_length = 100
        _preload(conv)
        conv.submit("hi")
        await _settle(conv)

        messages = conv.agent.history.messages
        assert not any("compaction-summary" in m.content for m in messages)
        assert any("OLD-MARKER-0" in m.content for m in messages)
    finally:
        await manager.close()


# --------------------------------------------------------------------------
# 5. runtime.subagents.max_depth
# --------------------------------------------------------------------------

def _resolve(agent_id: str, *, depth: int, max_depth: int, defs, parent=None):
    return resolve_composition(
        agent_id,
        pool=[],
        preset=builtin_presets()["standard"],
        defs=defs,
        cwd=None,
        parent=parent,
        depth=depth,
        max_depth=max_depth,
    )


def test_max_depth_decides_who_may_still_delegate():
    from quickcode.subagents.definitions import builtin_defs

    defs = builtin_defs()

    # The default: the orchestrator delegates, and so does its child.
    assert _resolve(ORCHESTRATOR_ID, depth=0, max_depth=2, defs=defs).spawns
    assert _resolve("general", depth=0, max_depth=2, defs=defs).spawns

    # One level: the child it spawns is a leaf.
    assert _resolve(ORCHESTRATOR_ID, depth=0, max_depth=1, defs=defs).spawns
    assert not _resolve("general", depth=0, max_depth=1, defs=defs).spawns

    # Zero levels means no delegation at all, including from the orchestrator.
    assert not _resolve(ORCHESTRATOR_ID, depth=0, max_depth=0, defs=defs).spawns


async def test_max_depth_refuses_a_spawn_below_the_limit(tmp_path):
    deps = SubagentDeps(
        provider=FakeProvider([]),
        profile=None,
        env=None,
        mode_getter=lambda: Mode.ask,
        cwd=tmp_path,
        depth=1,
        limits=RuntimeLimits(max_depth=1),
    )
    with pytest.raises(ValueError, match="depth limit"):
        await spawn_subagent(deps, agent_type="general", prompt="p")


# --------------------------------------------------------------------------
# 6. runtime.subagents.max_agents
# --------------------------------------------------------------------------

async def test_max_agents_is_the_declared_setting(tmp_path):
    deps = SubagentDeps(
        provider=FakeProvider([]),
        profile=None,
        env=None,
        mode_getter=lambda: Mode.ask,
        cwd=tmp_path,
        depth=0,
        limits=RuntimeLimits(max_agents=2),
    )
    deps.spawned.extend(["general-1", "general-2"])
    with pytest.raises(ValueError, match=r"subagent limit reached \(2 "):
        await spawn_subagent(deps, agent_type="general", prompt="p")


# --------------------------------------------------------------------------
# the two backstops
# --------------------------------------------------------------------------

def test_a_setting_cannot_raise_a_backstop_above_its_declared_maximum(tmp_path):
    _write(tmp_path, "runtime.subagents", max_depth=99, max_agents=100_000)
    limits = runtime_limits(tmp_path)
    assert limits.max_depth == manifest.core_setting("runtime.subagents", "max_depth").maximum
    assert limits.max_agents == manifest.core_setting("runtime.subagents", "max_agents").maximum
    assert (limits.max_depth, limits.max_agents) == (4, 500)


def test_a_setting_may_still_lower_a_backstop(tmp_path):
    _write(tmp_path, "runtime.subagents", max_depth=1, max_agents=3)
    limits = runtime_limits(tmp_path)
    assert (limits.max_depth, limits.max_agents) == (1, 3)


def test_nonsense_values_fall_back_rather_than_raise(tmp_path):
    _write(tmp_path, "runtime.agent_loop", max_rounds="not a number")
    _write(tmp_path, "runtime.compaction", threshold=-5, keep_turns=999)
    limits = runtime_limits(tmp_path)
    assert limits.max_rounds == 50
    assert limits.compaction_threshold == 0.3   # the declared minimum
    assert limits.keep_turns == 20              # the declared maximum


# --------------------------------------------------------------------------
# freezing
# --------------------------------------------------------------------------

async def test_an_edit_does_not_reach_a_session_already_running(tmp_path):
    _write(tmp_path, "runtime.agent_loop", max_rounds=2)
    provider = LoopingProvider()
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        assert conv.agent.limits.max_rounds == 2

        # The user edits the setting while the conversation is open.
        _write(tmp_path, "runtime.agent_loop", max_rounds=9)
        assert runtime_limits(tmp_path).max_rounds == 9
        assert conv.agent.limits.max_rounds == 2

        conv.submit("go")
        await _settle(conv)
        assert provider.requests == 3  # the budget it opened with, not 10

        # A session opened after the edit gets the new value.
        assert manager.open("fresh").agent.limits.max_rounds == 9
    finally:
        await manager.close()


# --------------------------------------------------------------------------
# the tool badge
# --------------------------------------------------------------------------

def test_a_tool_is_not_locked_just_because_read_only_is_a_fact():
    from quickcode.tools.registry import default_registry

    specs = manifest.tool_specs(list(default_registry().tools.values()))
    assert specs
    for spec in specs:
        assert spec.tier() == "free", f"{spec.id} still badges {spec.tier()}"
        # The fact itself stays fixed: nothing may write it.
        read_only = spec.setting("read_only")
        assert read_only is not None and read_only.tier == "locked" and read_only.fact

    # A real knob still decides the badge.
    subagents = next(s for s in manifest.core_specs() if s.id == "runtime.subagents")
    assert subagents.tier() == "locked"  # sanitize_reports is not a fact
