"""Command hooks as a ``LoopHook``: the user's scripts, at the loop's seams.

Nothing here adds a seam to the loop. Each event is one of the ``LoopHook``
methods every hook already has, which is the point: plan mode and a user's
guard script are the same kind of thing, differing only in who wrote them.

======================  ============================  =========================
Event                   Seam                          What it can do
======================  ============================  =========================
``SessionStart``        first ``before_turn``         add context
``UserPromptSubmit``    ``before_turn``               refuse the turn, add context
``PreToolUse``          ``check_tool``                deny, or require a prompt
``PostToolUse``         ``tool_feedback``             send the model a note
``Stop``                ``turn_done``                 run, and be recorded
======================  ============================  =========================

``PreToolUse`` runs *after* the permission engine and can only tighten its
answer (``core.hooks.tighten``). A hook that says "allow" skips nothing: not a
prompt, not a protected path, not a circuit breaker.

The configuration is read once per session, when it first matters -- at the
first turn -- and then held, so granting trust to a project before typing the
first message is enough for its hooks to run in that conversation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from quickcode.checkpoints.hook import CheckpointHook
from quickcode.core.hooks import LoopHook, ToolCheck, default_hooks
from quickcode.core.permissions import Decision
from quickcode.hooks.config import EVENTS, HookCommand, HookConfig, load_hooks
from quickcode.hooks.events import HookRun
from quickcode.hooks.protocol import Verdict, interpret
from quickcode.hooks.runner import run_command
from quickcode.prompts.system import system_reminder

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance
    from quickcode.core.events import AssembledToolCall
    from quickcode.tools.base import Tool

log = logging.getLogger("quickcode.hooks")

T = TypeVar("T")


# What happens to the thing a hook was about when the hook itself fails.
_FAILED_OPEN = {
    "PreToolUse": " The call went ahead.",
    "UserPromptSubmit": " The message was sent.",
}


def _notice(event: str, tool: str, verdict: Verdict) -> str:
    """The transcript line for this run, or "" when there is nothing to say.

    A blocked tool call has none: its result already says so, in the card the
    user is looking at.
    """
    what = f"{event} hook" + (f" on {tool}" if tool else "")
    parts: list[str] = []
    if verdict.outcome in ("error", "timeout"):
        detail = f"failed ({verdict.reason})" if verdict.outcome == "error" else verdict.reason
        parts.append(f"{what} {detail}.{_FAILED_OPEN.get(event, '')}")
    elif verdict.outcome == "block":
        if event == "UserPromptSubmit":
            parts.append(f"Message not sent. A UserPromptSubmit hook refused it: "
                         f"{verdict.reason}")
        elif event in ("Stop", "SessionStart"):
            parts.append(f"{what} exited 2 ({verdict.reason}), but a {event} hook "
                         "cannot block anything.")
    elif verdict.outcome == "ask":
        parts.append(f"{what} asked for your confirmation"
                     + (f": {verdict.reason}" if verdict.reason else "."))
    if verdict.message:
        parts.append(verdict.message)
    return " ".join(parts)


async def _unless_interrupted(agent: AgentInstance, work: Awaitable[T]) -> T | None:
    """``work``'s result, or None if the turn is interrupted first.

    Stop has to reach a hook the way it reaches a running command: a Stop hook
    that runs the test suite must not hold the turn open for its full timeout
    after the user asked it to end. Cancelling the work kills the hooks' trees.
    """
    task = asyncio.ensure_future(work)
    waiter = asyncio.ensure_future(agent._cancel.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return None


class CommandHooks(LoopHook):
    id = "hook.command_hooks"

    def __init__(
        self,
        load: Callable[[], HookConfig],
        *,
        session_id: str = "",
        transcript_path: str = "",
        resumed: bool = False,
        subagent: bool = False,
    ) -> None:
        self._load = load
        self._config: HookConfig | None = None
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.resumed = resumed
        # A subagent's turns are the parent model's delegations, not the
        # user's messages, so only the tool events apply to one.
        self.subagent = subagent
        self._started = False

    @property
    def config(self) -> HookConfig:
        if self._config is None:
            try:
                self._config = self._load()
            except Exception as exc:  # a settings file must not take the turn down
                log.warning("hooks could not be loaded: %s", exc)
                self._config = HookConfig()
        return self._config

    def for_subagent(self) -> CommandHooks:
        """The same hooks, for a child: its tool calls are gated the same way.

        A guard that stopped at the orchestrator would be one delegation away
        from not being a guard.
        """
        child = CommandHooks(lambda: self.config, session_id=self.session_id,
                             transcript_path=self.transcript_path, subagent=True)
        child._config = self.config
        return child

    # -- running -----------------------------------------------------------

    async def _run(
        self, agent: AgentInstance, event: str, hooks: list[HookCommand],
        fields: dict[str, Any], tool: str = "",
    ) -> list[Verdict] | None:
        """Run every matching hook at once. None if the turn was interrupted."""
        payload = {
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "cwd": str(agent.ctx.cwd),
            "hook_event_name": event,
            "permission_mode": agent.mode.value,
            "agent_name": agent.name,
            **fields,
        }
        call_id = str(fields.get("tool_use_id", ""))

        async def one(hook: HookCommand) -> Verdict:
            try:
                done = await run_command(hook.command, payload=payload, ctx=agent.ctx,
                                         timeout_s=hook.timeout_s)
            except asyncio.CancelledError:
                agent.bus.emit(HookRun(event=event, outcome="interrupted", hook_id=hook.id,
                                       scope=hook.scope, tool=tool, call_id=call_id))
                raise
            verdict = interpret(event, exit_code=done.exit_code, stdout=done.stdout,
                                stderr=done.stderr, timed_out=done.timed_out,
                                timeout_s=hook.timeout_s, spawn_error=done.spawn_error)
            agent.bus.emit(HookRun(
                event=event, outcome=verdict.outcome, hook_id=hook.id, scope=hook.scope,
                tool=tool, call_id=call_id, decision=verdict.decision,
                exit_code=done.exit_code,
                ms=done.ms, reason=verdict.reason,
                notice=_notice(event, tool, verdict),
            ))
            return verdict

        return await _unless_interrupted(agent, asyncio.gather(*(one(h) for h in hooks)))

    def _report_refused(self, agent: AgentInstance) -> None:
        refused = self.config.refused
        if not refused:
            return
        events = sorted({h.event for h in refused}, key=EVENTS.index)
        n = len(refused)
        agent.bus.emit(HookRun(
            event="SessionStart", outcome="refused", scope="project",
            notice=(f"This project declares {n} {'hook' if n == 1 else 'hooks'} "
                    f"({', '.join(events)}) and is not trusted, so "
                    f"{'it was' if n == 1 else 'they were'} not run. Trust the "
                    "project to let its hooks run in new conversations."),
        ))

    # -- the seams ---------------------------------------------------------

    async def before_turn(self, agent: AgentInstance, text: str) -> str | None:
        if self.subagent:
            return None
        if not self._started:
            self._started = True
            self._report_refused(agent)
            start = self.config.matching("SessionStart")
            if start:
                source = "resume" if self.resumed else "startup"
                for verdict in await self._run(agent, "SessionStart", start,
                                               {"source": source}) or ():
                    if verdict.context:
                        agent.queue_reminder(
                            f"Context from a SessionStart hook:\n{verdict.context}")

        hooks = self.config.matching("UserPromptSubmit")
        if not hooks:
            return None
        verdicts = await self._run(agent, "UserPromptSubmit", hooks, {"prompt": text})
        if verdicts is None:
            return "interrupted"
        refusals = [v.reason for v in verdicts if v.outcome == "block"]
        if refusals:
            return "\n".join(refusals)
        for verdict in verdicts:
            if verdict.context:
                agent.queue_reminder(
                    f"Context from a UserPromptSubmit hook:\n{verdict.context}")
        return None

    async def check_tool(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, decision: Decision,
    ) -> ToolCheck | None:
        hooks = self.config.matching("PreToolUse", tool.name)
        if not hooks:
            return None
        verdicts = await self._run(
            agent, "PreToolUse", hooks,
            {"tool_name": tool.name, "tool_input": args, "tool_use_id": call.id},
            tool=tool.name,
        )
        if verdicts is None:
            return ToolCheck(Decision.deny, "[interrupted]")
        denied = [v.reason for v in verdicts if v.outcome == "block"]
        if denied:
            return ToolCheck(Decision.deny,
                             "Blocked by a PreToolUse hook: " + "\n".join(denied))
        asked = [v.reason for v in verdicts if v.outcome == "ask"]
        if asked:
            return ToolCheck(Decision.ask, "\n".join(r for r in asked if r))
        return None

    async def tool_feedback(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, content: str, is_error: bool,
    ) -> str | None:
        hooks = self.config.matching("PostToolUse", tool.name)
        if not hooks:
            return None
        verdicts = await self._run(
            agent, "PostToolUse", hooks,
            {"tool_name": tool.name, "tool_input": args, "tool_use_id": call.id,
             "tool_response": {"content": content, "is_error": is_error}},
            tool=tool.name,
        )
        notes: list[str] = []
        for verdict in verdicts or ():
            if verdict.outcome == "block" and verdict.reason:
                notes.append(verdict.reason)
            if verdict.context:
                notes.append(verdict.context)
        if not notes:
            return None
        return system_reminder("PostToolUse hook feedback:\n" + "\n\n".join(notes))

    async def turn_done(self, agent: AgentInstance, text: str) -> None:
        if self.subagent:
            return
        hooks = self.config.matching("Stop")
        if hooks:
            await self._run(agent, "Stop", hooks,
                            {"stop_hook_active": False, "last_assistant_message": text})


def session_hooks(
    cwd: Path | None,
    *,
    session_id: str = "",
    transcript_path: str = "",
    resumed: bool = False,
    trusted: bool | None = None,
) -> list[LoopHook]:
    """The loop hooks for one session: the built-ins, then the user's commands.

    File checkpoints sit ahead of the user's commands, so a PreToolUse hook
    that refuses a call cannot keep the turn from being counted, and every
    change a PostToolUse hook is told about has already been saved.
    """
    hooks: list[LoopHook] = [*default_hooks()]
    checkpoints = CheckpointHook.for_session(cwd, session_id)
    if checkpoints is not None:
        hooks.append(checkpoints)
    hooks.append(CommandHooks(lambda: load_hooks(cwd, trusted=trusted), session_id=session_id,
                              transcript_path=transcript_path, resumed=resumed))
    return hooks


def child_hooks(parent: list[LoopHook] | None) -> list[LoopHook]:
    """The loop hooks for a subagent spawned under ``parent``'s session."""
    hooks = default_hooks()
    for hook in parent or ():
        if isinstance(hook, (CheckpointHook, CommandHooks)):
            hooks.append(hook.for_subagent())
    return hooks
