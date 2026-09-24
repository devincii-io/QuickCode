"""A subagent is held to the session's deny and ask rules.

The child's engine was built with ``Rules()``, so a rule the user wrote held
for the orchestrator and for nobody it delegated to: under an auto-edit or
yolo effective mode a child wrote to `prod/` past a `write(prod/**)` deny, or
ran what a `bash(curl **)` deny forbade (docs/COMPLIANCE.md §7.5, W4).
"""

from __future__ import annotations

import json
from pathlib import Path

from quickcode.config import Environment, Profile
from quickcode.core.events import TextDelta, ToolCallEnd, ToolCallStart, TurnDone
from quickcode.core.permissions import Decision, Mode, Rules
from quickcode.subagents.runner import SubagentDeps, spawn_subagent

SESSION_RULES = Rules(
    allow=["bash(curl **)"],
    ask=["bash(git push**)"],
    deny=["write(prod/**)", "bash(rm **)"],
)


class WritesProd:
    """A child that tries to write under `prod/` once, then reports."""

    def __init__(self) -> None:
        self.sent = False

    async def stream_chat(self, req):
        if not self.sent:
            self.sent = True
            args = json.dumps({"file_path": "prod/config.yml", "content": "pwned"})
            yield ToolCallStart("c1", "write")
            yield ToolCallEnd("c1", "write", args)
            yield TurnDone("tool_calls")
        else:
            yield TextDelta("done")
            yield TurnDone("stop")

    async def list_models(self):
        return []


def _deps(provider, root: Path, mode: Mode) -> SubagentDeps:
    return SubagentDeps(
        provider=provider,
        profile=Profile(),
        env=Environment.detect(root),
        mode_getter=lambda: mode,
        cwd=root,
        rules_getter=lambda: SESSION_RULES,
    )


async def test_a_child_takes_the_sessions_deny_and_ask_rules_and_no_allow(tmp_path):
    deps = _deps(WritesProd(), tmp_path, Mode.yolo)
    agent_id, _, _ = await spawn_subagent(deps, agent_type="general", prompt="go")
    child = deps.roster[agent_id].permissions
    assert child.rules.deny == SESSION_RULES.deny
    assert child.rules.ask == SESSION_RULES.ask
    assert child.rules.allow == []
    assert child.evaluate("bash", "rm -rf build") == Decision.deny


async def test_a_child_cannot_write_where_the_session_may_not(tmp_path):
    deps = _deps(WritesProd(), tmp_path, Mode.auto_edit)
    await spawn_subagent(deps, agent_type="general", prompt="go")
    assert not (tmp_path / "prod" / "config.yml").exists()


async def test_a_grandchild_is_handed_the_same_rules(tmp_path):
    deps = _deps(WritesProd(), tmp_path, Mode.yolo)
    agent_id, _, _ = await spawn_subagent(deps, agent_type="general", prompt="go")
    grandchild = deps.child(2, deps.roster[agent_id].permissions, self_id=agent_id)
    assert grandchild.rules_getter is not None
    assert grandchild.rules_getter().deny == SESSION_RULES.deny
