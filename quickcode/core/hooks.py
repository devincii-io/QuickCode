"""Loop hooks: the lifecycle seams a plugin can sit in.

The loop used to know about specific tools by name. It hid ``write`` and
``edit`` in plan mode and routed a call named ``plan`` to the UI, which meant
plan mode was a property of the loop rather than of anything you could
inspect, disable or replace. Those two behaviours now live in a hook, and the
loop only knows that hooks exist.

A hook may:

* narrow the tools offered to the model for a request (``visible_tools``),
* answer a tool call itself instead of running the tool (``intercept``),
* tighten the permission engine's answer for a call (``check_tool``) -- turn an
  allow into an ask or a deny, never the other way,
* look at a finished result (``after_tool``) -- for surfacing, never for
  changing the answer, which the model has already been promised,
* add a note the model reads *after* that answer (``tool_feedback``),
* refuse a user turn before it reaches the model (``before_turn``),
* hear that a turn finished on its own (``turn_done``).

Hooks run in list order. The first hook to intercept a call wins. The helpers at
the bottom are what the loop calls, so it holds one line per seam rather than
the rules for combining several hooks' answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from quickcode.core.permissions import DEFAULT_SPEC, Decision, Mode

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance
    from quickcode.core.events import AssembledToolCall
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


@dataclass
class ToolCheck:
    """A hook's view of a call the permission engine has already decided.

    Only ``ask`` and ``deny`` can have any effect: ``tighten`` keeps whichever
    of the engine's answer and the hook's is stricter, so a hook that says
    ``allow`` is recorded and changes nothing.
    """

    decision: Decision
    reason: str = ""


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

    async def check_tool(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, decision: Decision,
    ) -> ToolCheck | None:
        return None

    async def tool_feedback(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, content: str, is_error: bool,
    ) -> str | None:
        return None

    async def before_turn(self, agent: AgentInstance, text: str) -> str | None:
        """None lets the turn run; a string refuses it and says why."""
        return None

    async def turn_done(self, agent: AgentInstance, text: str) -> None:
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
        plan_md = args.get("plan")
        # Checked here because an interception runs before the tool's own
        # schema is: a missing or non-text plan went to the review as it was.
        if not isinstance(plan_md, str) or not plan_md.strip():
            return Interception(
                "The plan tool needs the full plan as markdown text in `plan`.",
                is_error=True,
            )
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


_STRICTNESS = {Decision.allow: 0, Decision.ask: 1, Decision.deny: 2}


async def tighten(
    agent: AgentInstance, call: AssembledToolCall, tool: Tool, args: dict,
    decision: Decision,
) -> tuple[Decision, str]:
    """The engine's decision after every hook has had its say, and why.

    Stricter only. A hook runs after the engine, sees what it decided, and can
    move an allow to an ask or anything to a deny; it can never turn an ask or
    a deny into an allow. That is what keeps a protected path, a circuit
    breaker or a deny rule out of reach of anything a hook prints. A call the
    engine already denied is not shown to the hooks at all.

    An ask in ``dontask`` becomes a deny, the way the engine's own asks do in
    that mode: its whole promise is that nothing stops to wait for a person.
    """
    reason = ""
    tightened = False
    for hook in agent.hooks:
        if decision is Decision.deny:
            break
        check = await hook.check_tool(agent, call, tool, args, decision)
        if check is not None and _STRICTNESS[check.decision] > _STRICTNESS[decision]:
            decision, reason, tightened = check.decision, check.reason, True
    if tightened and decision is Decision.ask and agent.mode is Mode.dontask:
        decision = Decision.deny
        reason = ("A hook asked for confirmation, and dontask mode refuses whatever "
                  "would ask." + (f" {reason}" if reason else ""))
    return decision, reason


async def with_feedback(
    agent: AgentInstance, call: AssembledToolCall, tool: Tool, args: dict,
    content: str, is_error: bool,
) -> str:
    """The result as the model will read it: the tool's answer, then any notes.

    Appended, never substituted -- the answer is still the tool's.
    """
    notes: list[str] = []
    for hook in agent.hooks:
        note = await hook.tool_feedback(agent, call, tool, args, content, is_error)
        if note:
            notes.append(note)
    return "\n\n".join([content, *notes]) if notes else content


async def refused_turn(agent: AgentInstance, text: str) -> str | None:
    """Why a hook turned this user message away, or None to run the turn."""
    for hook in agent.hooks:
        refusal = await hook.before_turn(agent, text)
        if refusal is not None:
            return refusal
    return None


async def turn_finished(agent: AgentInstance, text: str) -> None:
    for hook in agent.hooks:
        await hook.turn_done(agent, text)
