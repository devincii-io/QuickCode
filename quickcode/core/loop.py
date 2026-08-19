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
from quickcode.prompts.system import system_reminder
from quickcode.providers.base import ChatRequest, ProviderError
from quickcode.tools.base import clean_text

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
    # Only when it is news. A reminder earns its tokens by reporting a change;
    # the mode used to be restated on every turn for the life of the session,
    # which is a fixed cost per request for a sentence the model already had.
    if (mode_note := agent.take_mode_change()):
        reminders.append(system_reminder(mode_note))
    # Anything else queued since the last turn -- one delivery each, in order.
    reminders.extend(system_reminder(r) for r in agent.take_reminders())
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
        if msg is None:
            # An error surfaced itself (``AgentStatus("error")``); a cancel had
            # nothing to surface it with, so a client that saw the stream stop
            # mid-sentence was told nothing at all -- and the recorder never
            # flushed the half-streamed text, which then reappeared glued to
            # the *next* turn's assistant message.
            if agent.cancelled:
                agent.bus.emit(AgentStatus("interrupted"))
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


def _abandon_round(
    agent: AgentInstance,
    usage: Usage,
    calls: dict[str, dict[str, str]],
    ended: list[str],
    reason: str,
) -> None:
    """Close the books on a round that ends without an assistant message.

    Two things are already public by the time a round is abandoned, and both
    outlive the coroutine that dropped them. The provider's ``usage`` was
    emitted, so it is in the log and a resume counts it -- if the live ledger
    skips it, the same session adds up to two different numbers depending on
    who is reading. And every tool call that finished streaming was emitted as
    a ``tool_call``, so a reader holds a call with no result: a spinner that
    never stops, live and on replay alike.
    """
    if usage.input_tokens or usage.output_tokens or usage.cached_tokens or usage.cost_usd:
        agent.ledger.add(usage)
    for cid in ended:
        agent.bus.emit(
            ToolResultEvent(cid, calls.get(cid, {}).get("name", ""), reason, True, 0, {})
        )


async def _stream_once(agent: AgentInstance) -> AssistantMessage | None:
    req = ChatRequest(
        model=agent.model,
        messages=agent.history.build_messages(),
        tools=_tools_for(agent),
        max_tokens=agent.max_tokens or None,
        temperature=agent.temperature,
    )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[str, dict[str, str]] = {}
    order: list[str] = []
    # The subset of ``order`` that reached the wire as a whole ``tool_call``.
    ended: list[str] = []
    usage = Usage()
    finish = "stop"
    agent.bus.emit(AgentStatus("streaming"))
    try:
        async for ev in agent.provider.stream_chat(req):
            if agent.cancelled:
                _abandon_round(agent, usage, calls, ended, "[interrupted]")
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
                if ev.id not in ended:
                    ended.append(ev.id)
            elif isinstance(ev, Usage):
                usage = ev
            elif isinstance(ev, TurnDone):
                finish = ev.finish_reason
                if ev.error:
                    # The provider event was already emitted above. Flip state
                    # and stop without duplicating the same error in the UI.
                    _abandon_round(agent, usage, calls, ended, "[round failed]")
                    agent.bus.emit(AgentStatus("error"))
                    return None
    except ProviderError as e:
        # Emit the error once (TurnDone carries the text); AgentStatus only
        # flips the state indicator so it is not rendered a second time.
        agent.bus.emit(TurnDone("error", str(e)))
        _abandon_round(agent, usage, calls, ended, "[round failed]")
        agent.bus.emit(AgentStatus("error"))
        return None
    except BaseException:
        # A provider that raises something other than ProviderError, or a
        # cancelled task: the exception is the caller's to handle, but the
        # round's public leftovers are still ours to close.
        _abandon_round(agent, usage, calls, ended, "[round failed]")
        raise

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
    """Read-only calls run concurrently; mutating calls sequentially in order.

    Every call that got this far was announced to the world as a ``tool_call``.
    ``record`` is the only way a result is written down here, and the ``finally``
    sweep guarantees it happens once for each of them -- an interrupt, a
    cancelled gather, a hook that raised, all of it. A ``tool_call`` with no
    ``tool_result`` is a spinner nothing ever stops, and it is just as permanent
    on replay as it was live.
    """
    results: dict[str, tuple[str, bool]] = {}

    def record(
        call: AssembledToolCall,
        content: str,
        is_error: bool,
        *,
        ms: int = 0,
        ui_meta: dict | None = None,
    ) -> None:
        if call.id in results:
            return
        results[call.id] = (content, is_error)
        agent.bus.emit(
            ToolResultEvent(call.id, call.name, content, is_error, ms, ui_meta or {})
        )

    async def run_one(call: AssembledToolCall) -> None:
        started = asyncio.get_running_loop().time()
        content, is_error, ui_meta = await _run_tool(agent, call)
        ms = int((asyncio.get_running_loop().time() - started) * 1000)
        record(call, content, is_error, ms=ms, ui_meta=ui_meta)

    readonly: list[AssembledToolCall] = []
    mutating: list[AssembledToolCall] = []
    for c in calls:
        tool = agent.registry.get(c.name)
        (readonly if (tool and tool.is_read_only) else mutating).append(c)

    try:
        if readonly and not agent.cancelled:
            running = asyncio.gather(*(run_one(c) for c in readonly))
            interrupted = asyncio.create_task(agent._cancel.wait())
            done, _ = await asyncio.wait(
                {running, interrupted}, return_when=asyncio.FIRST_COMPLETED
            )
            if interrupted in done and agent.cancelled:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            else:
                interrupted.cancel()
                await asyncio.gather(interrupted, return_exceptions=True)
                # A tool that raised past ``_run_tool``'s own catch (a hook,
                # say) leaves its failure on the gather. Take it so asyncio
                # does not report it unretrieved; the sweep writes the result.
                if running.done() and not running.cancelled():
                    running.exception()
        for c in mutating:
            if agent.cancelled:
                break
            tool = agent.registry.get(c.name)
            if not (tool and tool.interruptible):
                # Not interruptible on purpose: a `write` or `edit` stopped
                # halfway leaves a truncated file, which is worse than a slow
                # Stop. These are also fast.
                await run_one(c)
                continue
            # Interruptible — race it against the cancel the same way the
            # read-only batch is raced. Awaiting it outright is what made Stop
            # look ignored: the flag was set, the transcript said "(interrupt
            # requested)" as many times as it was pressed, and the loop stayed
            # parked inside a `find /` until the command's own timeout. The
            # tool kills its child on the way out.
            running = asyncio.ensure_future(run_one(c))
            interrupted = asyncio.create_task(agent._cancel.wait())
            done, _ = await asyncio.wait(
                {running, interrupted}, return_when=asyncio.FIRST_COMPLETED
            )
            if interrupted in done and agent.cancelled:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                break
            interrupted.cancel()
            await asyncio.gather(interrupted, return_exceptions=True)
            if running.done() and not running.cancelled():
                running.exception()   # see the read-only branch above
    finally:
        for c in calls:
            record(c, "[interrupted]" if agent.cancelled else "[no result]", True)

    return [(c, *results[c.id]) for c in calls]


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
            call_id=call.id,
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

    # Cleaned here rather than in each tool, because "the result must be
    # encodable" is a property of this boundary, not of any one tool. What
    # crosses it goes to three places that all encode UTF-8 -- the session log,
    # the WebSocket, and the provider request -- and a lone surrogate from a
    # command's output used to raise in the recorder, long after the tool had
    # returned, killing the turn with the command still shown as running. An
    # MCP server or a plugin tool can hand back the same thing.
    content = clean_text(result.content)

    for hook in agent.hooks:
        hook.after_tool(agent, tool, content, result.is_error)
    return (content, result.is_error, result.ui_meta)
