"""The agentic loop — a single-turn driver.

Per-turn state machine: idle → sending → streaming → executing_tools → (loop).
Rules that matter (docs/ARCHITECTURE §The agent loop):
  - All tool results return in a single batch (one push), never split.
  - Read-only tools run concurrently; mutating tools sequentially in call order.
  - Failed tools still return a result with is_error so the model can recover.
  - Loop guard: ``runtime.agent_loop.max_rounds`` tool rounds (50 by default),
    then a wrap-up reminder.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from quickcode.core.events import (
    AgentStatus,
    AssembledToolCall,
    AssistantMessage,
    ContextInjection,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
    TurnDone,
    Usage,
)
from quickcode.core.permissions import Decision
from quickcode.kernel.composition import RuntimeLimits
from quickcode.prompts.system import mode_reminder, system_reminder
from quickcode.providers.base import ChatRequest, ProviderError

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance

# The fallback budget, for an agent built without resolved limits. What a turn
# actually spends is ``agent.limits.max_rounds``, resolved once per session
# from ``runtime.agent_loop.max_rounds``; the number itself is declared in the
# manifest and reaches here through ``RuntimeLimits``.
MAX_ROUNDS = RuntimeLimits().max_rounds


async def run_turn(agent: AgentInstance, user_input: str) -> str:
    reminders: list[str] = []
    if agent.take_post_compaction():
        from quickcode.prompts.compact import POST_COMPACTION_REMINDER

        reminders.append(system_reminder(POST_COMPACTION_REMINDER))
    if (mode_note := mode_reminder(agent.mode.value)):
        reminders.append(system_reminder(mode_note))
    for r in reminders:
        agent.bus.emit(ContextInjection(r))
    agent.history.push_user(user_input, reminders or None)
    last_text = ""
    # Read once per turn, off the session's frozen limits: a settings edit
    # mid-turn must not move the budget under a turn already counting.
    max_rounds = max(1, int(getattr(agent, "limits", RuntimeLimits()).max_rounds))
    for round_no in range(max_rounds + 1):
        if round_no == max_rounds:
            wrap_up = system_reminder(
                "You are over the iteration budget. Wrap up: report state and next steps."
            )
            agent.bus.emit(ContextInjection(wrap_up))
            agent.history.push_user("", [wrap_up])
        agent.bus.emit(AgentStatus("sending"))
        msg = await _stream_once(agent)
        if msg is None:  # cancelled or error already surfaced
            return last_text
        agent.history.push_assistant(msg)
        agent.ledger.add(msg.usage)
        if msg.text:
            last_text = msg.text
        if not msg.tool_calls:
            agent.bus.emit(AgentStatus("idle"))
            return last_text

        agent.bus.emit(AgentStatus("executing_tools"))
        results = await _execute_tools(agent, msg.tool_calls)
        agent.history.push_tool_results(results)
        if agent.cancelled:
            agent.bus.emit(AgentStatus("interrupted"))
            return last_text
    return last_text


def _tools_for(agent: AgentInstance):
    """The tools offered to the model for this request.

    Structural filtering is a hook's business (plan mode withholds the
    mutating tools -- docs/PERMISSIONS §Plan mode). The loop just asks each
    hook to narrow the list and sends what survives.
    """
    tools = list(agent.registry.tools.values())
    for hook in agent.hooks:
        tools = hook.visible_tools(agent, tools)
    return [t.schema() for t in tools]


async def _stream_once(agent: AgentInstance) -> AssistantMessage | None:
    req = ChatRequest(
        model=agent.model,
        messages=agent.history.build_messages(),
        tools=_tools_for(agent),
    )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[str, dict[str, str]] = {}
    order: list[str] = []
    usage = Usage()
    finish = "stop"
    agent.bus.emit(AgentStatus("streaming"))
    try:
        async for ev in agent.provider.stream_chat(req):
            if agent.cancelled:
                return None
            agent.bus.emit(ev)
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
            elif isinstance(ev, ReasoningDelta):
                reasoning_parts.append(ev.text)
            elif isinstance(ev, ToolCallStart):
                calls.setdefault(ev.id, {"name": ev.name, "args": ""})
                if ev.id not in order:
                    order.append(ev.id)
            elif isinstance(ev, ToolCallDelta):
                calls.setdefault(ev.id, {"name": "", "args": ""})
                calls[ev.id]["args"] += ev.arguments
                if ev.id not in order:
                    order.append(ev.id)
            elif isinstance(ev, ToolCallEnd):
                c = calls.setdefault(ev.id, {"name": ev.name, "args": ""})
                if ev.name:
                    c["name"] = ev.name
                if ev.arguments and not c["args"]:
                    c["args"] = ev.arguments
                if ev.id not in order:
                    order.append(ev.id)
            elif isinstance(ev, Usage):
                usage = ev
            elif isinstance(ev, TurnDone):
                finish = ev.finish_reason
                if ev.error:
                    # The provider event was already emitted above. Flip state
                    # and stop without duplicating the same error in the UI.
                    agent.bus.emit(AgentStatus("error"))
                    return None
    except ProviderError as e:
        # Emit the error once (TurnDone carries the text); AgentStatus only
        # flips the state indicator so it is not rendered a second time.
        agent.bus.emit(TurnDone("error", str(e)))
        agent.bus.emit(AgentStatus("error"))
        return None

    tool_calls = [
        AssembledToolCall(id=cid, name=calls[cid]["name"], arguments=calls[cid]["args"] or "{}")
        for cid in order
    ]
    return AssistantMessage(
        text="".join(text_parts),
        reasoning="".join(reasoning_parts),
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=usage,
    )


async def _execute_tools(
    agent: AgentInstance, calls: list[AssembledToolCall]
) -> list[tuple[AssembledToolCall, str, bool]]:
    """Read-only calls run concurrently; mutating calls sequentially in order."""
    results: dict[str, tuple[str, bool]] = {}

    async def run_one(call: AssembledToolCall) -> None:
        started = asyncio.get_running_loop().time()
        content, is_error, ui_meta = await _run_tool(agent, call)
        ms = int((asyncio.get_running_loop().time() - started) * 1000)
        results[call.id] = (content, is_error)
        agent.bus.emit(
            ToolResultEvent(call.id, call.name, content, is_error, ms, ui_meta)
        )

    readonly: list[AssembledToolCall] = []
    mutating: list[AssembledToolCall] = []
    for c in calls:
        tool = agent.registry.get(c.name)
        (readonly if (tool and tool.is_read_only) else mutating).append(c)

    if readonly:
        if agent.cancelled:
            for c in readonly:
                results[c.id] = ("[interrupted]", True)
        else:
            running = asyncio.gather(*(run_one(c) for c in readonly))
            interrupted = asyncio.create_task(agent._cancel.wait())
            done, _ = await asyncio.wait(
                {running, interrupted}, return_when=asyncio.FIRST_COMPLETED
            )
            if interrupted in done and agent.cancelled:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                for c in readonly:
                    results.setdefault(c.id, ("[interrupted]", True))
            else:
                interrupted.cancel()
                await asyncio.gather(interrupted, return_exceptions=True)
    for c in mutating:
        if agent.cancelled:
            results[c.id] = ("[interrupted]", True)
            continue
        await run_one(c)

    return [(c, *results.get(c.id, ("[no result]", True))) for c in calls]


async def _run_tool(
    agent: AgentInstance, call: AssembledToolCall
) -> tuple[str, bool, dict]:
    tool = agent.registry.get(call.name)
    if tool is None:
        return (f"Unknown tool: {call.name}", True, {})
    try:
        raw = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as e:
        return (f"Invalid tool arguments (not JSON): {e}", True, {})

    # A hook may answer the call itself -- that is how plan review works.
    for hook in agent.hooks:
        taken = await hook.intercept(agent, tool, raw)
        if taken is not None:
            return (taken.content, taken.is_error, taken.ui_meta)
    try:
        inp = tool.Input(**raw)
    except Exception as e:  # pydantic validation
        return (f"Invalid arguments for {call.name}: {e}", True, {})

    # Permission gate. The tool declares which argument is the target and how
    # it wants to be gated; the engine no longer recognises tools by name.
    decision, arg_target = agent.permissions.evaluate_tool(tool, raw)
    if decision == Decision.ask:
        from quickcode.core.agent import PermissionRequest

        req = PermissionRequest(
            tool=call.name,
            arg=arg_target,
            rule_suggestion=agent.permissions.suggest_rule(call.name, arg_target),
            preview=tool.render_call(inp),
            agent_name=agent.name,
        )
        outcome = await agent.permission_cb(req)
        if not outcome.allow:
            reason = outcome.deny_message or "User denied this action."
            return (f"Permission denied by user: {reason}", True, {})
        if outcome.persist:
            agent.permissions.rules.persist_allow(agent.ctx.cwd, req.rule_suggestion)
    elif decision == Decision.deny:
        return ("Blocked by permission rules or current mode.", True, {})

    try:
        result = await tool.run(inp, agent.ctx)
    except Exception as e:  # tools never crash the loop
        return (f"Tool {call.name} raised: {type(e).__name__}: {e}", True, {})

    for hook in agent.hooks:
        hook.after_tool(agent, tool, result.content, result.is_error)
    return (result.content, result.is_error, result.ui_meta)
