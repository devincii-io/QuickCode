"""Loop hooks: the lifecycle seams a plugin can sit in.

The loop used to know about specific tools by name. It hid ``write`` and
``edit`` in plan mode and routed a call named ``plan`` to the UI, which meant
plan mode was a property of the loop rather than of anything you could
inspect, disable or replace. Those two behaviours now live in a hook, and the
loop only knows that hooks exist.

A hook may:

* narrow the tools offered to the model for a request (``visible_tools``),
* answer a tool call itself instead of running the tool (``intercept``),
* look at a finished result (``after_tool``) -- for surfacing, never for
  changing the answer, which the model has already been promised.

Hooks run in list order. The first hook to intercept a call wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from quickcode.core.permissions import DEFAULT_SPEC, Mode

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance
    from quickcode.tools.base import Tool

# The tool the plan protocol is built around. It belongs to the hook that
# implements plan mode, not to the loop.
PLAN_TOOL = "plan"


@dataclass
class Interception:
    """A hook's answer, standing in for the tool's own result."""

    content: str
    is_error: bool = False
    ui_meta: dict[str, Any] = field(default_factory=dict)


class LoopHook:
    """Base class. Every method has a do-nothing default."""

    id: ClassVar[str] = ""

    def visible_tools(self, agent: AgentInstance, tools: list[Tool]) -> list[Tool]:
        return tools

    async def intercept(
        self, agent: AgentInstance, tool: Tool, args: dict
    ) -> Interception | None:
        return None

    def after_tool(
        self, agent: AgentInstance, tool: Tool, content: str, is_error: bool
    ) -> None:
        return None


class PlanModeHook(LoopHook):
    """Plan mode, in one place.

    In plan mode the agent investigates and designs but changes nothing, so
    the mutating tools are withheld outright rather than offered and then
    denied -- a tool the model can see is a tool it will try. Shell tools are
    the exception: they are only partly mutating, and the permission engine
    decomposes them per subcommand so read-only commands still work.

    The ``plan`` tool is the inverse: offered only in plan mode, and answered
    by the UI's plan review rather than by the tool itself.
    """

    id = "hook.plan_mode"

    def visible_tools(self, agent: AgentInstance, tools: list[Tool]) -> list[Tool]:
        in_plan = agent.mode == Mode.plan
        out: list[Tool] = []
        for tool in tools:
            if tool.name == PLAN_TOOL:
                if in_plan:
                    out.append(tool)
                continue
            spec = getattr(tool, "permission", DEFAULT_SPEC)
            if in_plan and spec.mutates and not spec.shell:
                continue
            out.append(tool)
        return out

    async def intercept(
        self, agent: AgentInstance, tool: Tool, args: dict
    ) -> Interception | None:
        if tool.name != PLAN_TOOL:
            return None
        plan_md = args.get("plan", "")
        if agent.plan_cb is None:
            agent.approved_plan = plan_md
            return Interception("Plan recorded (no interactive review available).")

        outcome = await agent.plan_cb(plan_md)
        if outcome.approved:
            if outcome.mode_after is not None:
                agent.set_mode(outcome.mode_after)
            agent.approved_plan = plan_md
            return Interception(
                f"Plan approved. Proceeding in {agent.mode.value} mode. "
                "Execute the plan now."
            )
        feedback = outcome.feedback.strip() or "(no feedback given)"
        return Interception(
            f"Plan not approved. Stay in plan mode and revise. Feedback: {feedback}"
        )


def default_hooks() -> list[LoopHook]:
    return [PlanModeHook()]
