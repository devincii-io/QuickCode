"""The review desk: permission and plan requests waiting on a person.

Review round-trips over the WebSocket. The agent awaits an ``asyncio.Future``
that a ``permission_decision`` / ``plan_decision`` client message resolves;
until then the request sits here, where the state event lists it, the switch
guard refuses to move under it, and an interrupt answers it.

A ``Conversation`` owns one desk and keeps its long-standing method names
(``permission_cb``, ``resolve_permission`` ...) as thin delegates.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quickcode.core.agent import PermissionOutcome, PermissionRequest, PlanOutcome
from quickcode.core.permissions import Mode


@dataclass
class PendingReview:
    """A permission or plan request awaiting a client decision."""

    req_id: str
    kind: str  # "permission" | "plan"
    payload: dict[str, Any]
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]


class ReviewDesk:
    """Every review a conversation has open, and the events that bracket each.

    ``emit`` is the conversation's (broadcast, and log when loggable);
    ``on_plan_resolved`` is called once a plan answer has been announced, so the
    state the composer draws from catches up with the mode the plan chose.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], Any],
                 on_plan_resolved: Callable[[], None]) -> None:
        self.pending: dict[str, PendingReview] = {}
        self._emit = emit
        self._on_plan_resolved = on_plan_resolved

    def listing(self) -> list[dict[str, Any]]:
        """What the state event says is waiting, one entry per open review."""
        return [
            {"req_id": p.req_id, "kind": p.kind, **p.payload}
            for p in self.pending.values()
        ]

    async def permission(self, req: PermissionRequest) -> PermissionOutcome:
        req_id = uuid.uuid4().hex[:10]
        payload = {
            "tool": req.tool,
            "arg": req.arg,
            "rule_suggestion": req.rule_suggestion,
            "preview": req.preview,
            "agent": req.agent_name,
            "call_id": req.call_id,
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = PendingReview(req_id, "permission", payload, fut)
        self._emit({"type": "permission_request", "req_id": req_id, **payload})
        try:
            outcome: PermissionOutcome = await fut
        finally:
            self.pending.pop(req_id, None)
        self._emit(
            {
                "type": "permission_resolved",
                "req_id": req_id,
                "allow": outcome.allow,
                "persist": outcome.persist,
                "tool": req.tool,
                "arg": req.arg,
                "call_id": req.call_id,
            }
        )
        return outcome

    async def plan(self, plan_md: str) -> PlanOutcome:
        req_id = uuid.uuid4().hex[:10]
        payload = {"plan": plan_md}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = PendingReview(req_id, "plan", payload, fut)
        self._emit({"type": "plan_request", "req_id": req_id, **payload})
        try:
            outcome: PlanOutcome = await fut
        finally:
            self.pending.pop(req_id, None)
        self._emit(
            {
                "type": "plan_resolved",
                "req_id": req_id,
                "approved": outcome.approved,
                "mode_after": outcome.mode_after.value if outcome.mode_after else None,
                "feedback": outcome.feedback,
            }
        )
        self._on_plan_resolved()
        return outcome

    def resolve_permission(self, req_id: str, *, allow: bool, persist: bool,
                           deny_message: str) -> bool:
        p = self.pending.get(req_id)
        if p is None or p.kind != "permission" or p.future.done():
            return False
        p.future.set_result(
            PermissionOutcome(allow=allow, persist=persist, deny_message=deny_message)
        )
        return True

    def resolve_plan(self, req_id: str, *, approved: bool, mode_after: str | None,
                     feedback: str) -> bool:
        p = self.pending.get(req_id)
        if p is None or p.kind != "plan" or p.future.done():
            return False
        mode = None
        if mode_after:
            with contextlib.suppress(ValueError):
                mode = Mode(mode_after)
        p.future.set_result(PlanOutcome(approved=approved, mode_after=mode, feedback=feedback))
        return True

    def deny_all(self) -> int:
        """Answer every review still waiting on the user. Returns how many.

        The cancel flag is read by the loop, and a turn parked on a permission
        modal is not in the loop -- it is awaiting a ``Future`` that only a
        client decision resolves. Nothing was going to resolve it once the user
        pressed Stop, so the turn never ended: ``busy`` stayed true, the Stop
        button stayed on screen and the composer stayed disabled with nothing
        actually running. Denying is the honest answer -- the user just said no
        to the whole turn -- and it also emits the ``permission_resolved`` /
        ``plan_resolved`` half of the pair, which is what closes the modal.
        """
        answered = 0
        for p in list(self.pending.values()):
            if p.future is None or p.future.done():
                continue
            if p.kind == "permission":
                p.future.set_result(PermissionOutcome(
                    allow=False,
                    deny_message="Interrupted by the user before this was answered.",
                ))
            else:
                p.future.set_result(PlanOutcome(
                    approved=False, feedback="Interrupted by the user."
                ))
            answered += 1
        return answered
