"""The agentic loop — a single-turn driver.

Per-turn state machine: idle → sending → streaming → executing_tools → (loop).
Rules that matter (docs/ARCHITECTURE §The agent loop):
  - All tool results return in a single batch (one push), never split.
  - Consecutive read-only tools run concurrently; any other call is a barrier
    that runs alone, in call order.
  - Failed tools still return a result with is_error so the model can recover.
  - Loop guard: ``runtime.agent_loop.max_rounds`` tool rounds (50 by default),
    then a wrap-up reminder.
  - Context guard: every request is checked against the window first, and a
    refusal for length is answered by shrinking and one retry
    (``core/context_guard.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from quickcode.core.context_guard import before_request, recover_from_overflow
from quickcode.core.events import (
    AgentStatus,
    AssembledToolCall,
    AssistantMessage,
    ContextInjection,
    ReasoningBlock,
    ReasoningDelta,
    SystemNote,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultEvent,
    TurnDone,
    Usage,
)
from quickcode.core.hooks import refused_turn, tighten, turn_finished, with_feedback
from quickcode.core.permissions import Decision
from quickcode.kernel.composition import RuntimeLimits
from quickcode.prompts.system import system_reminder
from quickcode.providers.base import ChatRequest, ProviderError
from quickcode.providers.overflow import is_context_overflow
from quickcode.tools.base import clean_text

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance


async def run_turn(agent: AgentInstance, user_input: str) -> str:
    # Refused before it is pushed: the model never sees a message a hook
    # turned away, and the hook has already said why (docs/HOOKS.md).
    if await refused_turn(agent, user_input) is not None:
        agent.bus.emit(AgentStatus("interrupted" if agent.cancelled else "idle"))
        return ""
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
        # Between rounds, where a compaction cannot part a call from its result.
        compacted = await _unless_cancelled(agent, before_request(agent))
        if agent.cancelled:
            agent.bus.emit(AgentStatus("interrupted"))
            return last_text
        if round_no == max_rounds:
            wrap_up = system_reminder(
                "You are over the iteration budget. Wrap up: report state and next steps."
            )
            agent.bus.emit(ContextInjection(wrap_up))
            agent.history.push_user("", [wrap_up])
        agent.bus.emit(AgentStatus("sending"))
        msg = await _stream_once(agent, compacted=bool(compacted))
        if msg is None:
            # An error surfaced itself (``AgentStatus("error")``); a cancel had
            # nothing to surface it with, so a client that saw the stream stop
            # mid-sentence was told nothing at all -- and the recorder never
            # flushed the half-streamed text, which then reappeared glued to
            # the *next* turn's assistant message.
            if agent.cancelled:
                agent.bus.emit(AgentStatus("interrupted"))
            return last_text
        # A reply with neither text nor calls is not kept: an empty assistant
        # message is refused by some backends on every later request, which
        # would wedge the conversation over a response that said nothing.
        if msg.text or msg.tool_calls:
            agent.history.push_assistant(msg)
        agent.ledger.add(msg.usage)
        if msg.text:
            last_text = msg.text
        if not msg.tool_calls:
            await turn_finished(agent, last_text)
            agent.bus.emit(AgentStatus("idle"))
            return last_text

        agent.bus.emit(AgentStatus("executing_tools"))
        results: dict[str, tuple[str, bool]] = {}
        try:
            await _execute_tools(agent, msg, results)
        finally:
            # Pushed even when this task is being cancelled: the assistant
            # message above is already in history, a call without a result is
            # a request every provider refuses, and a cancelled subagent is
            # still resumable with ``send_message``.
            agent.history.push_tool_results(
                [(c, *results.get(c.id, ("[no result]", True))) for c in msg.tool_calls]
            )
        if agent.cancelled:
            agent.bus.emit(AgentStatus("interrupted"))
            return last_text
    # The wrap-up round asked for tools anyway. They ran; the turn still ends.
    await turn_finished(agent, last_text)
    agent.bus.emit(AgentStatus("idle"))
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


async def _unless_cancelled(agent: AgentInstance, aw: Awaitable) -> Any:
    """``aw``'s result, or None if the agent was interrupted first."""
    out: list = []

    async def run() -> None:
        out.append(await aw)

    return out[0] if await _until_cancelled(agent, run()) else None


async def _until_cancelled(agent: AgentInstance, aw: Awaitable) -> bool:
    """Await ``aw`` unless the agent is interrupted first. True if it finished.

    An interrupt cancels the work, and so does cancelling the task this runs
    in: either way nothing is left running with nobody waiting on it. A
    failure of the work itself is re-raised.
    """
    work = asyncio.ensure_future(aw)
    stop = asyncio.ensure_future(agent._cancel.wait())
    try:
        await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, stop, return_exceptions=True)
    if work.cancelled():
        if not agent.cancelled:
            # Cancelled by something other than an interrupt -- the task that
            # owns this turn going away. That is not a Stop; it propagates.
            raise asyncio.CancelledError
        return False
    work.result()
    return True


class _Round:
    """What one provider request has produced, assembled as it streams in."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        # Signed reasoning blocks, replayed verbatim on the next request and
        # never shown (providers/anthropic).
        self.reasoning_blocks: list[dict] = []
        # call id -> ([name], argument chunks), in the order the calls began.
        # Chunks are joined once at the end: a large ``write`` streams its
        # content in thousands of pieces, and appending each to one string
        # copied the whole argument every time.
        self.calls: dict[str, tuple[list[str], list[str]]] = {}
        # The calls that reached the wire whole, as a ``tool_call``.
        self.ended: dict[str, None] = {}
        self.usage = Usage()
        self.finish = "stop"
        self.error: str | None = None
        # A refusal for length the caller asked to see before anyone else.
        self.held: str | None = None
        # True once the provider's stream ran out on its own.
        self.complete = False

    def silent(self) -> bool:
        """Nothing of this round has been shown: a retry would repeat nothing."""
        return not (self.text or self.reasoning or self.calls)

    def _call(self, cid: str, name: str = "") -> tuple[list[str], list[str]]:
        return self.calls.setdefault(cid, ([name], []))

    def take(self, ev) -> None:
        if isinstance(ev, TextDelta):
            self.text.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            self.reasoning.append(ev.text)
        elif isinstance(ev, ToolCallStart):
            self._call(ev.id, ev.name)
        elif isinstance(ev, ToolCallDelta):
            self._call(ev.id)[1].append(ev.arguments)
        elif isinstance(ev, ToolCallEnd):
            name, args = self._call(ev.id, ev.name)
            if ev.name:
                name[0] = ev.name
            if ev.arguments and not args:
                args.append(ev.arguments)
            self.ended[ev.id] = None
        elif isinstance(ev, Usage):
            self.usage = ev
        elif isinstance(ev, TurnDone):
            self.finish = ev.finish_reason
            if ev.error:
                self.error = ev.error

    def message(self) -> AssistantMessage:
        return AssistantMessage(
            text="".join(self.text),
            reasoning="".join(self.reasoning),
            tool_calls=[
                AssembledToolCall(id=cid, name=name[0], arguments="".join(args) or "{}")
                for cid, (name, args) in self.calls.items()
            ],
            finish_reason=self.finish,
            usage=self.usage,
            reasoning_blocks=self.reasoning_blocks,
        )

    def abandon(self, agent: AgentInstance, reason: str) -> None:
        """Close the books on a round that ends without an assistant message.

        Two things are already public by the time a round is abandoned, and
        both outlive the coroutine that dropped them. The provider's ``usage``
        was emitted, so it is in the log and a resume counts it -- if the live
        ledger skips it, the same session adds up to two different numbers
        depending on who is reading. And every tool call that finished
        streaming was emitted as a ``tool_call``, so a reader holds a call with
        no result: a spinner that never stops, live and on replay alike.
        """
        u = self.usage
        if u.input_tokens or u.output_tokens or u.cached_tokens or u.cost_usd:
            agent.ledger.add(u)
        for cid in self.ended:
            name = self.calls[cid][0][0] if cid in self.calls else ""
            agent.bus.emit(ToolResultEvent(cid, name, reason, True, 0, {}))


@dataclass
class _TooLong:
    """A request the provider refused for length, before anything of it showed.

    Nothing about it has been surfaced yet -- no error on the bus, no state
    flip -- so a retry that succeeds leaves no trace of a failure.
    """

    error: str


async def _stream_once(agent: AgentInstance, *, compacted: bool = False) -> AssistantMessage | None:
    """One request, retried once if the provider refused it for length."""
    out = await _request(agent, hold_overflow=True)
    if not isinstance(out, _TooLong):
        return out
    shrunk = await _unless_cancelled(
        agent, recover_from_overflow(agent, out.error, compacted=compacted)
    )
    if shrunk:
        agent.bus.emit(AgentStatus("sending"))
        return await _request(agent, hold_overflow=False)
    if not agent.cancelled:
        agent.bus.emit(TurnDone("error", out.error))
        agent.bus.emit(AgentStatus("error"))
    return None


async def _request(
    agent: AgentInstance, *, hold_overflow: bool
) -> AssistantMessage | _TooLong | None:
    req = ChatRequest(
        model=agent.model,
        messages=agent.history.build_messages(),
        tools=_tools_for(agent),
        max_tokens=agent.max_tokens or None,
        temperature=agent.temperature,
    )
    rnd = _Round()
    agent.bus.emit(AgentStatus("streaming"))

    def too_long(error: str) -> bool:
        return hold_overflow and rnd.silent() and is_context_overflow(error)

    async def consume() -> None:
        stream = agent.provider.stream_chat(req)
        try:
            async for ev in stream:
                if agent.cancelled:
                    return
                if isinstance(ev, ReasoningBlock):
                    rnd.reasoning_blocks.append(ev.block)
                    continue
                if isinstance(ev, TurnDone) and ev.error and too_long(ev.error):
                    # Held back rather than emitted: the caller may yet recover.
                    rnd.held = ev.error
                    return
                agent.bus.emit(ev)
                rnd.take(ev)
                if rnd.error is not None:
                    return
            rnd.complete = True
        finally:
            # Closed now rather than whenever the generator is collected: an
            # open stream is a response the provider keeps generating, and
            # billing, for a turn nobody is reading any more.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    try:
        # Raced against the interrupt rather than checked between events: a
        # stream that goes quiet -- a model thinking without streaming it, a
        # stalled connection -- sends no event to check on, and Stop waited
        # for the next token or the read timeout.
        await _until_cancelled(agent, consume())
    except ProviderError as e:
        if too_long(str(e)):
            rnd.abandon(agent, "[round failed]")
            return _TooLong(str(e))
        # Emit the error once (TurnDone carries the text); AgentStatus only
        # flips the state indicator so it is not rendered a second time.
        agent.bus.emit(TurnDone("error", str(e)))
        rnd.abandon(agent, "[round failed]")
        agent.bus.emit(AgentStatus("error"))
        return None
    except BaseException:
        # A provider that raises something other than ProviderError, or a
        # cancelled task: the exception is the caller's to handle, but the
        # round's public leftovers are still ours to close.
        rnd.abandon(agent, "[round failed]")
        raise
    if rnd.held is not None:
        rnd.abandon(agent, "[round failed]")
        return _TooLong(rnd.held)
    if rnd.error is not None:
        # The provider's TurnDone was already emitted. Flip state and stop
        # without duplicating the same error in the UI.
        rnd.abandon(agent, "[round failed]")
        agent.bus.emit(AgentStatus("error"))
        return None
    if not rnd.complete:
        rnd.abandon(agent, "[interrupted]")
        return None
    return rnd.message()


def _batches(
    agent: AgentInstance, calls: list[AssembledToolCall]
) -> list[tuple[bool, list[AssembledToolCall]]]:
    """Split a round into what may run together, in call order.

    Consecutive read-only calls share a batch; every other call is a batch of
    its own and a barrier. Running all the reads first, as this once did, let
    a ``read`` issued after a ``write`` in the same response report the file
    as it was before the write.
    """
    out: list[tuple[bool, list[AssembledToolCall]]] = []
    for c in calls:
        tool = agent.registry.get(c.name)
        read_only = bool(tool and tool.is_read_only)
        if read_only and out and out[-1][0]:
            out[-1][1].append(c)
        else:
            out.append((read_only, [c]))
    return out


async def _execute_tools(
    agent: AgentInstance,
    msg: AssistantMessage,
    results: dict[str, tuple[str, bool]],
) -> None:
    """Run a round's calls, writing each one's result into ``results``.

    Every call that got this far was announced to the world as a ``tool_call``.
    ``record`` is the only way a result is written down here, and the ``finally``
    sweep guarantees it happens once for each of them -- an interrupt, a
    cancelled gather, a hook that raised, all of it. A ``tool_call`` with no
    ``tool_result`` is a spinner nothing ever stops, and it is just as permanent
    on replay as it was live.
    """
    calls = msg.tool_calls
    truncated = msg.finish_reason == "length"

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
        try:
            content, is_error, ui_meta = await _run_tool(agent, call, truncated=truncated)
        except Exception as e:  # a hook or the gate raised: say so, keep the siblings
            content, is_error, ui_meta = (
                f"Tool {call.name} failed: {type(e).__name__}: {e}", True, {}
            )
        ms = int((asyncio.get_running_loop().time() - started) * 1000)
        record(call, content, is_error, ms=ms, ui_meta=ui_meta)

    cancelled = False
    try:
        for read_only, batch in _batches(agent, calls):
            if agent.cancelled:
                break
            tool = agent.registry.get(batch[0].name)
            if not read_only and not (tool and tool.interruptible):
                # Not interruptible on purpose: a `write` or `edit` stopped
                # halfway leaves a truncated file, which is worse than a slow
                # Stop. These are also fast.
                await run_one(batch[0])
                continue
            # Raced against the cancel. Awaiting an interruptible call outright
            # is what made Stop look ignored: the flag was set, the transcript
            # said "(interrupt requested)" as many times as it was pressed, and
            # the loop stayed parked inside a `find /` until the command's own
            # timeout. The tool kills its child on the way out.
            if not await _until_cancelled(agent, asyncio.gather(*map(run_one, batch))):
                break
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # A cancelled task is an interrupt too, just not one the user pressed.
        interrupted = cancelled or agent.cancelled
        for c in calls:
            record(c, "[interrupted]" if interrupted else "[no result]", True)


def _permission_request(
    agent: AgentInstance, call: AssembledToolCall, tool, inp, raw: dict, target: str,
    hook_reason: str,
):
    """Everything the prompt shows: the call, the diff it would make, the exact
    rules "Always allow" would save, and the hook that asked, if one did."""
    from quickcode.core.agent import GatedCall, PermissionRequest

    gated = GatedCall(tool, raw, agent.permissions, agent.ctx.extra.get("bash_cwd"))
    offer = agent.permissions.suggest_rules(tool, raw, cwd=gated.cwd)
    try:
        diff = tool.render_diff(inp, agent.ctx)
    except Exception:  # a preview never stands between the user and the prompt
        diff = ""
    return PermissionRequest(
        tool=call.name,
        arg=target,
        rule_suggestion=", ".join(offer.rules),
        preview=tool.render_call(inp),
        agent_name=agent.name,
        call_id=call.id,
        rules=list(offer.rules),
        kept=[{"part": part, "reason": reason} for part, reason in offer.kept],
        diff=diff,
        hook_reason=hook_reason,
        gated=gated,
    )


async def _run_tool(
    agent: AgentInstance, call: AssembledToolCall, *, truncated: bool = False
) -> tuple[str, bool, dict]:
    tool = agent.registry.get(call.name)
    if tool is None:
        return (f"Unknown tool: {call.name}", True, {})
    try:
        raw = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as e:
        if truncated:
            # "Unterminated string" sends a model back to resend the same call,
            # which hits the same limit. The cause is what it can act on.
            return (
                "Invalid tool arguments: your response reached its output limit "
                "partway through this call, so the arguments were cut off. Send "
                "less in one call -- for a large file, write part of it and add "
                "the rest with edit.",
                True,
                {},
            )
        return (f"Invalid tool arguments (not JSON): {e}", True, {})
    if not isinstance(raw, dict):
        # Hooks and the permission gate read named arguments off a dict.
        return ("Invalid tool arguments: expected a JSON object of named arguments.", True, {})

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
    # it wants to be gated; the engine no longer recognises tools by name. A
    # shell's relative paths are relative to where its last `cd` left it.
    decision, arg_target = agent.permissions.evaluate_tool(
        tool, raw, cwd=agent.ctx.extra.get("bash_cwd")
    )
    # A hook may tighten that answer and never loosen it (hooks.tighten).
    decision, hook_reason = await tighten(agent, call, tool, raw, decision)
    if decision == Decision.ask:
        req = _permission_request(agent, call, tool, inp, raw, arg_target, hook_reason)
        outcome = await agent.permission_cb(req)
        if not outcome.allow:
            reason = outcome.deny_message or "User denied this action."
            return (f"Permission denied by user: {reason}", True, {})
        if outcome.persist:
            for rule in req.rules:
                unsaved = agent.permissions.rules.persist_allow(agent.ctx.cwd, rule)
                if unsaved:
                    agent.bus.emit(SystemNote(unsaved))
                    break
    elif decision == Decision.deny:
        return (hook_reason or "Blocked by permission rules or current mode.", True, {})

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
    content = await with_feedback(agent, call, tool, raw, content, result.is_error)
    return (content, result.is_error, result.ui_meta)
