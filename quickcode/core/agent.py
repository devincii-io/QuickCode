"""AgentInstance: the loop + history + token ledger + event bus.

One AgentInstance per conversation (and per subagent/teammate later). It runs
as an asyncio task, emits ``AgentEvent``s onto a bounded-fan-out bus, and awaits
permission decisions through an injected async callback (the UI supplies a
modal; headless supplies an auto-deny).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from quickcode.core.events import AgentEvent, Usage
from quickcode.core.history import History
from quickcode.core.loop import run_turn
from quickcode.core.permissions import Mode, PermissionEngine
from quickcode.kernel.composition import RuntimeLimits
from quickcode.providers.base import Provider
from quickcode.tools.base import ToolCtx


@dataclass
class PermissionRequest:
    tool: str
    arg: str
    rule_suggestion: str
    preview: str = ""
    agent_name: str = "main"


@dataclass
class PermissionOutcome:
    allow: bool
    persist: bool = False
    deny_message: str = ""


PermissionCallback = Callable[[PermissionRequest], Awaitable[PermissionOutcome]]


@dataclass
class PlanOutcome:
    """Result of a plan review, mapped by the UI from the PlanReviewModal.

    ``approved`` True with ``mode_after`` set means execution proceeds in that
    mode; False means keep planning and ``feedback`` steers the next turn.
    """

    approved: bool
    mode_after: Mode | None = None
    feedback: str = ""


PlanCallback = Callable[[str], Awaitable[PlanOutcome]]


class EventBus:
    """Fan-out with bounded per-subscriber queues (drop-to-resync on overflow)."""

    def __init__(self, maxsize: int = 2048) -> None:
        self._subs: list[asyncio.Queue[AgentEvent]] = []
        self._maxsize = maxsize
        self.overflowed: set[int] = set()

    def subscribe(self, maxsize: int | None = None) -> asyncio.Queue[AgentEvent]:
        # maxsize=0 (unbounded) for the sole UI consumer so a fast provider can't
        # overflow it and drop deltas (which render as garbled/missing text).
        # Per-frame work is capped by the drain loop's coalescing, not the queue.
        size = self._maxsize if maxsize is None else maxsize
        q: asyncio.Queue[AgentEvent] = asyncio.Queue(size)
        self._subs.append(q)
        return q

    def emit(self, ev: AgentEvent) -> None:
        for q in self._subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self.overflowed.add(id(q))


def _usage_from_json(d: dict) -> Usage:
    """A logged usage record back into the event the ledger counts."""
    return Usage(
        input_tokens=int(d.get("input_tokens") or 0),
        output_tokens=int(d.get("output_tokens") or 0),
        cached_tokens=int(d.get("cached_tokens") or 0),
        cost_usd=d.get("cost_usd"),
    )


@dataclass
class Ledger:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    # The most recent request's footprint — this is the live context size
    # (the cumulative fields above measure session spend, not context).
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    # How much of the cumulative totals above was spent by subagents. Kept
    # apart only so the UI can attribute a fan-out; the money is already
    # counted in the totals.
    subagent_input_tokens: int = 0
    subagent_output_tokens: int = 0
    subagent_cost_usd: float = 0.0

    def add(self, u: Usage) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cached_tokens += u.cached_tokens
        self.last_input_tokens = u.input_tokens
        self.last_output_tokens = u.output_tokens
        if u.cost_usd:
            self.cost_usd += u.cost_usd

    def add_subagent(self, u: Usage) -> None:
        """Count a child's usage as session spend, but never as context.

        Money spent is money spent, so the cumulative fields take it. The
        ``last_*`` pair does not: it is the live footprint of *this* agent's
        last request, and a subagent's request went into a different context
        window entirely. Folding a fan-out into it would make the context meter
        (``context_pct``) read full while the parent's history is short, and
        would hand ``should_compact`` a number the parent never sent.
        """
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cached_tokens += u.cached_tokens
        self.subagent_input_tokens += u.input_tokens
        self.subagent_output_tokens += u.output_tokens
        if u.cost_usd:
            self.cost_usd += u.cost_usd
            self.subagent_cost_usd += u.cost_usd

    @classmethod
    def from_events(cls, events: list[dict]) -> Ledger:
        """Rebuild a session's spend from its logged ``usage`` events.

        What a session cost is a fact about the session, not about the process
        that happened to be running it — reopening one and being told it cost
        nothing is simply wrong.

        Subagent usage is logged one level down, inside the ``agent_event``
        wrapper that carries everything a child emitted, and is replayed
        through ``add_subagent`` so a resumed session splits spend from context
        exactly the way the live one did.
        """
        ledger = cls()
        for ev in events:
            kind = ev.get("type")
            if kind == "usage":
                ledger.add(_usage_from_json(ev))
            elif kind == "agent_event":
                inner = ev.get("ev") or {}
                if inner.get("type") == "usage":
                    ledger.add_subagent(_usage_from_json(inner))
        return ledger


class AgentInstance:
    def __init__(
        self,
        *,
        name: str,
        provider: Provider,
        registry,  # ToolRegistry (duck-typed: .schemas(), .get(name), .tools)
        history: History,
        ctx: ToolCtx,
        permissions: PermissionEngine,
        model: str,
        permission_cb: PermissionCallback,
        bus: EventBus | None = None,
        context_length: int | None = None,
        hooks: list | None = None,
        limits: RuntimeLimits | None = None,
    ) -> None:
        self.name = name
        self.provider = provider
        self.registry = registry
        self.history = history
        self.ctx = ctx
        self.permissions = permissions
        self.model = model
        self.permission_cb = permission_cb
        self.bus = bus or EventBus()
        # Loop lifecycle hooks: tool visibility and call interception. Plan
        # mode is one of these rather than a branch inside the loop.
        if hooks is None:
            from quickcode.core.hooks import default_hooks

            hooks = default_hooks()
        self.hooks = hooks
        # The session's frozen runtime numbers. Handed in at construction and
        # never re-read, so a settings edit reaches the next session rather
        # than a conversation already running. A direct embedder that passes
        # nothing gets the declared defaults.
        self.limits = limits or RuntimeLimits()
        self.ledger = Ledger()
        self.context_length = context_length
        self._cancel = asyncio.Event()
        self.busy = False
        self._post_compaction = False
        # Reminders wait here until the next turn opens and are delivered once.
        # A reminder describes a *change*; repeating an unchanged one every turn
        # spends tokens to tell the model something it was already told, and
        # trains it to skim the block that also carries the things that did
        # change.
        self._reminders: list[str] = []
        # The mode the model has actually been told about. None until the first
        # turn announces it.
        self._announced_mode: str | None = None
        # Optional hooks set by the app: called with a ChatMessage after each
        # message is appended (session persistence).
        self.on_message = None
        # Set by the app to review plans via the PlanReviewModal; None -> the
        # loop treats a plan call as recorded-without-review (headless).
        self.plan_cb = None
        self.approved_plan: str | None = None

    def mark_compacted(self) -> None:
        """Flag that the next user turn should carry a post-compaction reminder."""
        self._post_compaction = True
        # Compaction rewrites the transcript, so whatever the model was told
        # about the mode may not have survived into the summary. Forget that it
        # was announced and say it once more.
        self._announced_mode = None

    def take_post_compaction(self) -> bool:
        was = self._post_compaction
        self._post_compaction = False
        return was

    def queue_reminder(self, text: str) -> None:
        """Hold a reminder for the next turn. Duplicates collapse."""
        if text and text not in self._reminders:
            self._reminders.append(text)

    def take_reminders(self) -> list[str]:
        """Drain the queue. Called once as a turn opens."""
        out, self._reminders = self._reminders, []
        return out

    def take_mode_change(self) -> str:
        """The mode reminder body if the mode is news, else ''.

        Edge-triggered on purpose: the first turn announces the mode, and after
        that only a change does. Re-stating an unchanged mode on every turn was
        a fixed tax on every single request that told the model nothing.
        """
        current = self.permissions.mode.value
        if current == self._announced_mode:
            return ""
        self._announced_mode = current
        from quickcode.prompts.system import mode_reminder

        return mode_reminder(current)

    @property
    def mode(self) -> Mode:
        return self.permissions.mode

    def set_mode(self, mode: Mode) -> None:
        self.permissions.mode = mode

    def cancel(self) -> None:
        self._cancel.set()

    async def run_turn(self, user_input: str) -> str:
        self._cancel.clear()
        self.busy = True
        try:
            return await run_turn(self, user_input)
        finally:
            self.busy = False

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def context_pct(self) -> float | None:
        if not self.context_length:
            return None
        used = self.ledger.last_input_tokens + self.ledger.last_output_tokens
        return min(100.0, 100.0 * used / self.context_length)
