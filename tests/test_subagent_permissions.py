"""A subagent never holds more permission than the agent that spawned it.

The mode cap (``min(parent, ceiling)``) was only half of that. The other half
is the rules: a child used to get an empty ``Rules()``, so every ``deny`` and
``ask`` the user wrote stopped at the first delegation -- ``read(**.pem)``
denied to the orchestrator was one ``explore`` spawn away. And the cap was a
snapshot: a background child kept the mode it was spawned at after the user
had cycled the parent down to plan.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from quickcode.config import Environment, Profile
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.core.permissions import Decision, Mode, Rules
from quickcode.kernel.composition import RuntimeLimits
from quickcode.subagents.runner import SubagentDeps, spawn_subagent, spawn_subagent_background
from quickcode.tools.registry import default_registry
from tests.test_background_agents import _settle
from tests.test_server import FakeProvider, make_manager


def _deps(provider, cwd: Path, *, mode=lambda: Mode.auto_edit, rules=None,
          adopt=None) -> SubagentDeps:
    return SubagentDeps(
        provider=provider,
        profile=Profile(),
        env=Environment.detect(cwd),
        mode_getter=mode,
        rules_getter=(lambda: rules) if rules is not None else None,
        cwd=cwd,
        adopt_task=adopt,
        limits=RuntimeLimits(),
    )


def _tool_results(provider: FakeProvider, request_index: int) -> list[str]:
    msgs = provider.requests[request_index].messages
    return [m.content for m in msgs if m.role == "tool"]


async def test_a_deny_rule_the_user_wrote_still_holds_one_delegation_down(tmp_path):
    (tmp_path / ".quickcode").mkdir()
    (tmp_path / ".quickcode" / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["read(**.pem)"]}}), encoding="utf-8"
    )
    (tmp_path / "key.pem").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    spawn = json.dumps({"description": "look", "prompt": "read key.pem",
                        "agent_type": "explore"})
    provider = FakeProvider([
        [ToolCallEnd(id="p1", name="agent", arguments=spawn), TurnDone("tool_calls")],
        [ToolCallEnd(id="c1", name="read", arguments=json.dumps({"file_path": "key.pem"})),
         TurnDone("tool_calls")],
        [TextDelta("could not read it"), TurnDone("stop")],
        [TextDelta("done"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        assert conv.agent.permissions.evaluate("read", "key.pem") == Decision.deny
        conv.submit("delegate it")
        await _settle(conv)
    finally:
        await manager.close()

    child_saw = _tool_results(provider, 2)
    assert child_saw and "Blocked by permission rules" in child_saw[-1]
    assert not any("PRIVATE KEY" in r for r in child_saw)


async def test_an_ask_rule_is_never_waved_through_by_a_child(tmp_path):
    # The parent runs in yolo with an ask rule on pushes. A child that cannot
    # prompt must be refused, not handed the parent's mode without the rule.
    from quickcode.subagents.definitions import AgentDef, builtin_defs

    defs = dict(builtin_defs())
    defs["pusher"] = AgentDef("pusher", "p", tools=["bash"], mode_cap=Mode.yolo)
    provider = FakeProvider([
        [ToolCallEnd(id="c1", name="bash",
                     arguments=json.dumps({"command": "git push origin main"})),
         TurnDone("tool_calls")],
        [TextDelta("tried"), TurnDone("stop")],
    ])
    deps = _deps(provider, tmp_path, mode=lambda: Mode.yolo,
                 rules=Rules(ask=["bash(git push**)"]))
    deps.defs = defs

    await spawn_subagent(deps, agent_type="pusher", prompt="push it")

    results = _tool_results(provider, 1)
    assert results and "Permission denied" in results[-1]


async def test_a_nested_child_inherits_the_rules_too(tmp_path):
    from quickcode.subagents.definitions import AgentDef, builtin_defs

    defs = dict(builtin_defs())
    defs["lead"] = AgentDef("lead", "l", tools=None, spawns=["general"],
                            mode_cap=Mode.auto_edit)
    parent_rules = Rules(deny=["write(secret/**)"])
    deps = _deps(FakeProvider([]), tmp_path, rules=parent_rules)
    deps.defs = defs
    deps.pool = list(default_registry().tools.values())
    await spawn_subagent(deps, agent_type="lead", prompt="p")
    child = deps.roster["lead-1"]
    assert child.permissions.evaluate("write", "secret/x.txt") == Decision.deny

    nested = child.ctx.extra["subagent"]
    await spawn_subagent(nested, agent_type="general", prompt="p")
    grandchild = deps.roster["general-2"]
    assert grandchild.permissions.evaluate("write", "secret/x.txt") == Decision.deny
    assert grandchild.permissions.evaluate("write", "src/x.txt") == Decision.allow
    # Live all the way down: a rule the user adds now binds the grandchild.
    parent_rules.deny.append("write(src/**)")
    assert grandchild.permissions.evaluate("write", "src/x.txt") == Decision.deny


async def test_a_background_child_follows_the_parent_down_to_plan(tmp_path):
    """Shift+Tab to plan must stop a running background writer, not only the
    delegations that start after it."""
    parent_mode = {"now": Mode.auto_edit}
    gate = asyncio.Event()

    class Gated(FakeProvider):
        async def stream_chat(self, req):
            if len(self.requests) == 0:
                await gate.wait()
            async for ev in super().stream_chat(req):
                yield ev

    provider = Gated([
        [ToolCallEnd(id="c1", name="write",
                     arguments=json.dumps({"file_path": "out.txt", "content": "x"})),
         TurnDone("tool_calls")],
        [TextDelta("wrote it"), TurnDone("stop")],
    ])
    owned: list[asyncio.Task] = []
    deps = _deps(provider, tmp_path, mode=lambda: parent_mode["now"], adopt=owned.append)

    job = spawn_subagent_background(deps, agent_type="general", prompt="write out.txt")
    child = deps.roster[job.agent_id]
    assert child.mode == Mode.auto_edit

    parent_mode["now"] = Mode.plan
    assert child.mode == Mode.ask
    gate.set()
    await asyncio.gather(*owned)

    assert not (tmp_path / "out.txt").exists()
    results = _tool_results(provider, 1)
    assert results and "Permission denied" in results[-1]


async def test_a_child_never_rises_above_its_ceiling_when_the_parent_does(tmp_path):
    parent_mode = {"now": Mode.ask}
    deps = _deps(FakeProvider([]), tmp_path, mode=lambda: parent_mode["now"])
    await spawn_subagent(deps, agent_type="explore", prompt="p")
    child = deps.roster["explore-1"]

    parent_mode["now"] = Mode.yolo
    # explore is capped at ask by its definition, whatever the parent does.
    assert child.mode == Mode.ask
