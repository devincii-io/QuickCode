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

    def subscribe(self) -> asyncio.Queue[AgentEvent]:
        q: asyncio.Queue[AgentEvent] = asyncio.Queue(self._maxsize)
        self._subs.append(q)
        return q

    def emit(self, ev: AgentEvent) -> None:
        for q in self._subs:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                self.overflowed.add(id(q))


@dataclass
class Ledger:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, u: Usage) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cached_tokens += u.cached_tokens
        if u.cost_usd:
            self.cost_usd += u.cost_usd


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
        self.ledger = Ledger()
        self.context_length = context_length
        self._cancel = asyncio.Event()
        self.busy = False
        self._post_compaction = False
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

    def take_post_compaction(self) -> bool:
        was = self._post_compaction
        self._post_compaction = False
        return was

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
        used = self.ledger.input_tokens + self.ledger.output_tokens
        return min(100.0, 100.0 * used / self.context_length)
