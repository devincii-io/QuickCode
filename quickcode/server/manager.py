"""ConversationManager: live agent instances behind the web API.

One ``Conversation`` per conversation id. It owns the ``AgentInstance``, an
input queue (submits while busy are queued, parity with the old TUI), the
WebSocket attachments, and the append-only trace log. Every bus event is
broadcast live; assembled events (whole messages, tool calls, results,
decisions) are also persisted so replay reconstructs the identical transcript.

Permission and plan review round-trip over the WebSocket: the agent awaits an
``asyncio.Future`` that a ``permission_decision`` / ``plan_decision`` client
message resolves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.config import Config, Environment
from quickcode.core.agent import (
    AgentInstance,
    EventBus,
    Ledger,
    PermissionOutcome,
    PermissionRequest,
    PlanOutcome,
)
from quickcode.core.compact import run_compaction, should_compact
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.core.profiles import PermissionProfile
from quickcode.core.profiles import effective as effective_posture
from quickcode.core.tasks import TaskBoard
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    MODE_PRIVILEGE,
    ORCHESTRATOR_ID,
    Resolved,
    narrower_mode,
)
from quickcode.kernel.resolve import default_mode as resolved_default_mode
from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
from quickcode.prompts.system import render_system_prompt
from quickcode.providers.base import ModelInfo, Provider, ProviderError
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore
from quickcode.subagents.definitions import load_defs
from quickcode.subagents.runner import SubagentDeps
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import ToolRegistry

log = logging.getLogger("quickcode.server")

CLIENT_QUEUE_MAX = 4096


class SwitchRefused(Exception):
    """A composition switch that must not happen, carrying why.

    The frozen-composition invariant exists for two reasons and neither is "the
    composition may never change": the model has been told what tools it has,
    and the prompt cache breakpoint sits on the system message. Both survive a
    switch taken *between* turns. So a switch mid-turn is refused -- not queued,
    not applied on the next idle. A switch that lands invisibly three seconds
    later is worse than one that does not happen.
    """


@dataclass
class PendingReview:
    """A permission or plan request awaiting a client decision."""

    req_id: str
    kind: str  # "permission" | "plan"
    payload: dict[str, Any]
    future: asyncio.Future = field(repr=False, default=None)  # type: ignore[assignment]


class Client:
    """One attached WebSocket, fed through a bounded queue so a slow reader
    can't block the agent (overflow drops the client; it reconnects and
    replays from the log)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(CLIENT_QUEUE_MAX)
        self.overflowed = False

    def send(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.overflowed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(None)  # sentinel: disconnect to resync


class Conversation:
    def __init__(
        self,
        *,
        conv_id: str,
        agent: AgentInstance,
        store: SessionStore,
        board: TaskBoard,
        manager: ConversationManager,
        resolved: Resolved,
        preset_id: str = "",
        profile_id: str = "",
    ) -> None:
        self.conv_id = conv_id
        self.agent = agent
        self.store = store
        self.board = board
        self.manager = manager
        # Which composition this session is running *now* -- not the one it
        # opened with. A switch rewrites it and records it, so resume restores
        # the composition the session ended with.
        self.preset_id = preset_id
        # The permission posture this session runs under, by id. Unlike the
        # composition it is not frozen: a posture says what the session may do
        # on its own, and nothing the model has been told depends on it, so it
        # can be swapped mid-session without lying to anybody.
        self.profile_id = profile_id
        # The allow rules the posture put there, as opposed to the ones the user
        # accrued afterwards by answering "always allow". ``persist_allow``
        # appends to the live engine and writes to settings.local.json, but that
        # file's allow list is gated on project trust like every other -- so in
        # an untrusted project the grant exists *only* here, and swapping the
        # posture must carry it rather than recompute over it. See
        # ``apply_posture``.
        self._posture_allow = set(agent.permissions.rules.allow)
        # The session's frozen composition. Every value a running conversation
        # depends on -- the tool list, the section bodies, the ceiling, the
        # spawnable agents -- is read from here and nowhere else, so editing a
        # preset mid-flight cannot change the tools under a conversation that
        # has already been told what it has.
        self.resolved = resolved
        self.clients: set[Client] = set()
        self.pending: dict[str, PendingReview] = {}
        self.input_queue: list[str] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        # Detached subagent jobs. Kept apart from ``_tasks`` -- which holds the
        # two pumps that *are* the conversation -- because interrupt cancels
        # these and must not touch those. A task nobody holds is collected at
        # the end of the turn that created it, so this list is what makes a
        # background job a background job.
        self._jobs: list[asyncio.Task] = []
        # The session log itself — turn/seq stamping, assembly of the streamed
        # events, message persistence. Shared verbatim with the headless CLI so
        # `-p` and the UI cannot write two different kinds of session.
        self.rec = TranscriptRecorder(
            store,
            broadcast=self._broadcast,
            on_usage=self._emit_state,
            on_tasks_changed=self._emit_tasks,
            persisted=len(agent.history.messages),
            # Subagents each own a ``Ledger``; this is the one that adds up to
            # the session, so the recorder rolls their usage into it as it logs
            # it. Handed over as the object, not copied: a resume has already
            # replaced ``agent.ledger`` by the time we get here.
            ledger=agent.ledger,
        )

    def busy_reason(self) -> str | None:
        """Why this conversation must not be closed under the user, or None.

        "Live" used to mean "present in ``manager.conversations``", and nothing
        ever removes an entry from there — so a conversation was live from the
        moment it was first opened until the process exited. Deleting or
        archiving it, or removing its project, was refused for ever, with a
        message telling the user to close something that was not open. What
        actually matters is whether anyone is watching, whether a turn is
        running, and whether a detached job is still going.
        """
        if self.clients:
            return f"{len(self.clients)} window(s) attached"
        if self.agent.busy:
            return "a turn is running"
        if any(not t.done() for t in self._jobs):
            return "a background subagent job is running"
        if self.pending:
            return "a permission prompt is waiting for an answer"
        return None

    # ---- lifecycle ----
    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.rec.pump(self.agent.bus)))
        self._tasks.append(asyncio.create_task(self._worker()))

    async def close(self) -> None:
        self.agent.cancel()
        children = list(self.rec.child_pumps.values())
        jobs = list(self._jobs)
        for t in [*self._tasks, *children, *jobs]:
            t.cancel()
        await asyncio.gather(*self._tasks, *children, *jobs, return_exceptions=True)

    # ---- detached subagent jobs ----
    def adopt_job(self, task: asyncio.Task) -> None:
        """Take ownership of a background subagent task.

        Handed to the runner as ``SubagentDeps.adopt_task``. Finished tasks are
        dropped on the way in so a long conversation's list stays the set of
        jobs that are actually live.
        """
        self._jobs = [t for t in self._jobs if not t.done()]
        self._jobs.append(task)

    def _subagent_deps(self):
        return self.agent.ctx.extra.get("subagent") if self.agent.ctx else None

    def cancel_jobs(self) -> int:
        """Cancel every background job still in flight. Returns how many."""
        deps = self._subagent_deps()
        cancelled = deps.cancel_jobs() if deps is not None else 0
        for t in self._jobs:
            if not t.done():
                t.cancel()
        self._jobs = []
        return cancelled

    def _note_jobs_in_flight(self) -> None:
        """Never let a turn end with detached work silently outstanding.

        Two audiences, one moment. The user gets a transcript note, because a
        turn that looks finished while two subagents are still spending money
        is the surprise this whole feature could have shipped with. The model
        gets a queued reminder, delivered the way every other between-turn
        change is -- once, at the top of the next turn -- so the jobs it forgot
        about are the first thing it reads.
        """
        deps = self._subagent_deps()
        if deps is None:
            return
        running = deps.running_jobs()
        uncollected = deps.uncollected_jobs()
        if not running and not uncollected:
            return
        if running:
            ids = ", ".join(j.agent_id for j in running)
            self.agent.queue_reminder(
                f"{len(running)} background subagent job(s) are still running "
                f"({ids}). Call agent_status to check them and agent_result to "
                "collect each report; do not report the work as finished until "
                "you have."
            )
        if uncollected:
            ids = ", ".join(j.agent_id for j in uncollected)
            self.agent.queue_reminder(
                f"{len(uncollected)} background subagent job(s) have finished and "
                f"their reports are still uncollected ({ids}). Call agent_result "
                "on each one."
            )
        parts = []
        if running:
            parts.append(f"{len(running)} still running")
        if uncollected:
            parts.append(f"{len(uncollected)} finished, report uncollected")
        self.emit({
            "type": "system_note",
            "text": f"(background subagent jobs: {'; '.join(parts)})",
        })

    # ---- event fan-out ----
    def emit(self, ev: dict[str, Any], *, log_it: bool | None = None) -> dict[str, Any]:
        """Broadcast an event to all clients; persist it when loggable."""
        return self.rec.emit(ev, log_it=log_it)

    def _broadcast(self, ev: dict[str, Any]) -> None:
        text = json.dumps(ev, ensure_ascii=False)
        for c in list(self.clients):
            c.send(text)

    def _emit_tasks(self) -> None:
        self.emit(
            {"type": "tasks", "tasks": [t.to_dict() for t in self.board.list()]},
            log_it=False,
        )

    def _emit_state(self) -> None:
        self.emit(self.state_event(), log_it=False)

    def state_event(self) -> dict[str, Any]:
        a = self.agent
        return {
            "type": "state",
            "conv_id": self.conv_id,
            "model": a.model,
            "mode": a.mode.value,
            "busy": a.busy,
            "queued": len(self.input_queue),
            "context_pct": a.context_pct(),
            "context_length": a.context_length,
            "ledger": {
                "input_tokens": a.ledger.input_tokens,
                "output_tokens": a.ledger.output_tokens,
                "cached_tokens": a.ledger.cached_tokens,
                "cost_usd": a.ledger.cost_usd,
                # The part of the four numbers above that subagents spent —
                # already included in them, reported separately so the Usage
                # panel can say what a fan-out cost without re-deriving it.
                "subagent_input_tokens": a.ledger.subagent_input_tokens,
                "subagent_output_tokens": a.ledger.subagent_output_tokens,
                "subagent_cost_usd": a.ledger.subagent_cost_usd,
            },
            "pending": [
                {"req_id": p.req_id, "kind": p.kind, **p.payload}
                for p in self.pending.values()
            ],
            "tasks": [t.to_dict() for t in self.board.list()],
            # The posture, on the same event and for the same reason as the
            # composition: the composer draws a pill from it.
            "profile": self.profile_id,
            # The composition is session state, like the mode and the model, so
            # it rides on the same event the composer's other two pills read.
            "composition": {
                "id": self.preset_id,
                "ceiling": self.resolved.ceiling.value,
                "tools": len(self.resolved.tools),
                "denied": len(self.resolved.denied_tools),
                "spawns": list(self.resolved.spawns),
                "digest": self.resolved.digest(),
                "switchable": self.switch_blocked_reason() == "",
                "blocked_reason": self.switch_blocked_reason(),
            },
        }

    # ---- subagent bridging ----
    def on_subagent(self, agent_id: str, definition: str, bus: EventBus) -> None:
        self.rec.on_subagent(agent_id, definition, bus)

    def on_subagent_done(
        self, agent_id: str, definition: str, status: str, seconds: float = 0.0
    ) -> None:
        self.rec.on_subagent_done(agent_id, definition, status, seconds)
        self._emit_state()

    # ---- user input ----
    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.agent.busy:
            self.input_queue.append(text)
            self.emit({"type": "queued_message", "text": text}, log_it=False)
        else:
            self._inbox.put_nowait(text)
        self._emit_state()

    def cancel_pending_reviews(self) -> int:
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

    def interrupt(self) -> None:
        cleared = len(self.input_queue)
        self.input_queue.clear()
        if self.agent.busy:
            self.agent.cancel()
        reviews = self.cancel_pending_reviews()
        # Detached jobs are the whole point of the feature and exactly what an
        # interrupt is for: stopping the agent while its background children
        # kept spending would make Esc a lie.
        jobs = self.cancel_jobs()
        bits = []
        if cleared:
            bits.append(f"{cleared} queued message{'s' if cleared != 1 else ''} cleared")
        if jobs:
            bits.append(f"{jobs} background job{'s' if jobs != 1 else ''} cancelled")
        if reviews:
            bits.append(f"{reviews} pending review{'s' if reviews != 1 else ''} denied")
        note = "(interrupt requested)"
        if bits:
            note = f"(interrupt requested; {'; '.join(bits)})"
        self.emit({"type": "system_note", "text": note})
        self._emit_state()

    async def _worker(self) -> None:
        while True:
            text = await self._inbox.get()
            self.emit({"type": "user_message", "text": text})
            self._emit_state()
            try:
                await self.agent.run_turn(text)
            except Exception as e:  # never kill the worker
                log.exception("turn failed")
                self.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            # Everything after the turn is bookkeeping, and none of it is
            # allowed to be the reason a client never hears that the turn
            # ended: ``busy`` is cleared by a state event, so the state event
            # is emitted in a ``finally``. Without it, one raised summarization
            # left the Stop button on screen forever with nothing running.
            try:
                self.rec.persist_new_messages(self.agent)
                self._note_jobs_in_flight()
                # ``runtime.compaction.enabled`` gates the automatic path only:
                # /compact is a thing the user asked for, and switching the
                # automatic summary off is not a statement about that.
                limits = self.agent.limits
                if limits.compaction_enabled and should_compact(
                    self.agent, limits.compaction_threshold
                ):
                    await self._compact(manual=False)
            except Exception as e:  # never kill the worker
                log.exception("post-turn work failed")
                self.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                self._emit_state()
            # Send the next queued follow-up, if any.
            if self.input_queue and not self.agent.busy:
                self._inbox.put_nowait(self.input_queue.pop(0))

    # ---- compaction ----
    async def _compact(self, *, manual: bool) -> None:
        self.emit({"type": "status", "state": "sending", "detail": "compacting"}, log_it=False)
        try:
            summary = await run_compaction(
                self.agent, keep_turns=self.agent.limits.keep_turns
            )
        except ProviderError as e:
            self.emit({"type": "error", "message": f"compaction failed: {e}"})
            return
        # History was rebuilt wholesale; future messages persist from here.
        # The rebuilt history goes into the log as well, or the work is
        # undone by the next resume: `load_messages` would replay every
        # original message and hand the model exactly the context compaction
        # existed to remove.
        self.store.append_compaction(self.agent.history.messages)
        self.rec.persisted = len(self.agent.history.messages)
        self.emit({"type": "compacted", "summary_chars": len(summary), "manual": manual})
        self.emit(
            {"type": "system_note", "text": "(conversation compacted — earlier turns summarized)"}
        )
        self._emit_state()

    def request_compact(self) -> None:
        if self.agent.busy:
            self.emit({"type": "error", "message": "cannot compact while the agent is busy"})
            return
        asyncio.create_task(self._compact(manual=True))

    # ---- reviews (permission + plan) ----
    async def permission_cb(self, req: PermissionRequest) -> PermissionOutcome:
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
        self.emit({"type": "permission_request", "req_id": req_id, **payload})
        try:
            outcome: PermissionOutcome = await fut
        finally:
            self.pending.pop(req_id, None)
        self.emit(
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

    async def plan_cb(self, plan_md: str) -> PlanOutcome:
        req_id = uuid.uuid4().hex[:10]
        payload = {"plan": plan_md}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = PendingReview(req_id, "plan", payload, fut)
        self.emit({"type": "plan_request", "req_id": req_id, **payload})
        try:
            outcome: PlanOutcome = await fut
        finally:
            self.pending.pop(req_id, None)
        self.emit(
            {
                "type": "plan_resolved",
                "req_id": req_id,
                "approved": outcome.approved,
                "mode_after": outcome.mode_after.value if outcome.mode_after else None,
                "feedback": outcome.feedback,
            }
        )
        self._emit_state()
        return outcome

    def resolve_permission(self, req_id: str, *, allow: bool, persist: bool, deny_message: str) -> bool:
        p = self.pending.get(req_id)
        if p is None or p.kind != "permission" or p.future.done():
            return False
        p.future.set_result(
            PermissionOutcome(allow=allow, persist=persist, deny_message=deny_message)
        )
        return True

    def resolve_plan(self, req_id: str, *, approved: bool, mode_after: str | None, feedback: str) -> bool:
        p = self.pending.get(req_id)
        if p is None or p.kind != "plan" or p.future.done():
            return False
        mode = None
        if mode_after:
            with contextlib.suppress(ValueError):
                mode = Mode(mode_after)
        p.future.set_result(PlanOutcome(approved=approved, mode_after=mode, feedback=feedback))
        return True

    # ---- settings ----

    # What the user has to do about it, said once so the mode switch and the
    # profile switch cannot drift into two different explanations.
    YOLO_UNARMED = ("yolo mode is not enabled for this app — turn it on in "
                    "Settings → General")

    @property
    def yolo_allowed(self) -> bool:
        """Whether yolo is armed, asked of the app rather than of this session.

        The engine's ``yolo_accepted`` was frozen when the session opened, and
        the session someone arms yolo from is precisely the one they expect it
        to reach. Kept in step so nothing reading the engine disagrees.
        """
        allowed = self.manager.allow_yolo
        self.agent.permissions.yolo_accepted = allowed
        return allowed

    def set_mode(self, mode_str: str) -> None:
        try:
            mode = Mode(mode_str)
        except ValueError:
            self.emit({"type": "error", "message": f"unknown mode: {mode_str}"})
            return
        if mode == Mode.yolo and not self.yolo_allowed:
            self.emit({"type": "error", "message": self.YOLO_UNARMED})
            return
        ceiling = self.resolved.ceiling
        if MODE_PRIVILEGE[mode] > MODE_PRIVILEGE[ceiling]:
            # The mode is live, the ceiling is frozen. Without this a preset's
            # ``ceiling`` is decoration: it would say what the session may do
            # and then let anyone say otherwise.
            self.emit({
                "type": "error",
                "message": (f"this session is capped at {ceiling.value} by its "
                            f"composition; {mode.value} is above that"),
            })
            return
        self.agent.set_mode(mode)
        self.emit({"type": "mode_changed", "mode": mode.value})
        self._emit_state()

    def apply_posture(self, mode: Mode, rules: Rules,
                      profile: PermissionProfile | None) -> dict[str, Any]:
        """Adopt a permission profile on a session that is already running.

        Unlike a composition switch this is never refused and never waits for a
        turn boundary. Nothing in the conversation depends on the posture: the
        model was told which tools it has, not which of them will prompt, so
        rewriting the engine mid-turn changes the next gate check and nothing
        else. Making the user reopen the session to change what prompts would
        be the friction the pill exists to remove.

        The mode moves too. It is still capped by the composition's ceiling,
        and yolo still needs the app to have armed it -- a profile is a
        posture, not a way around either. What it no longer does is fail
        quietly: a mode it could not have is refused out loud.

        What is carried across rather than recomputed is the "always allow"
        the user answered *during this session*. It is in the live engine and,
        in a project nobody has trusted, nowhere else, so rebuilding the rule
        set from the file layer would revoke it -- the same revocation
        ``PermissionProfile.merged`` refuses to perform one layer up, and for
        the same reason: picking a posture is not a request to un-approve
        anything.
        """
        accrued = [r for r in self.agent.permissions.rules.allow
                   if r not in self._posture_allow]
        # Recorded before the carry, so it stays "what the posture contributed"
        # -- fold the accrued rules in here and the *next* switch would read
        # them as the posture's and drop them.
        self._posture_allow = set(rules.allow)
        rules = Rules(
            allow=list(rules.allow) + accrued,
            ask=list(rules.ask), deny=list(rules.deny),
        )
        self.agent.permissions.rules = rules
        self.profile_id = profile.id if profile else ""

        ceiling = self.resolved.ceiling
        asked = mode
        yolo_blocked = mode == Mode.yolo and not self.yolo_allowed
        if yolo_blocked:
            mode = Mode.ask
        capped = narrower_mode(mode, ceiling)
        # A profile whose mode could not be honoured used to land somewhere
        # else in complete silence, which reads as the profile not applying at
        # all -- the gate is right, the silence was the bug. Both refusals are
        # named, because a profile can hit them one after the other.
        reasons = []
        if yolo_blocked:
            reasons.append(self.YOLO_UNARMED)
        if capped != mode:
            reasons.append(f"this session is capped at {ceiling.value} by its composition")
        mode = capped
        if reasons:
            name = f"profile {profile.title}" if profile else "this profile"
            self.emit({
                "type": "system_note",
                "text": (f"{name} asks for {asked.value} mode: "
                         f"{'; '.join(reasons)}. Applied {mode.value} instead."),
            })
        if mode != self.agent.mode:
            self.agent.set_mode(mode)
            self.emit({"type": "mode_changed", "mode": mode.value})

        counts = (len(rules.allow), len(rules.ask), len(rules.deny))
        self.emit({
            "type": "profile_changed",
            "profile": self.profile_id,
            "title": profile.title if profile else "",
            "mode": mode.value,
            "allow": counts[0], "ask": counts[1], "deny": counts[2],
        }, log_it=True)
        self.emit({
            "type": "system_note",
            "text": (f"permission profile → {profile.title} "
                     f"(mode {mode.value}, {counts[0]} allow · {counts[1]} ask · "
                     f"{counts[2]} deny)") if profile else
                    "permission profile cleared; the project's own rules apply",
        })
        self._emit_state()
        return {
            "profile": self.profile_id, "mode": mode.value,
            "allow": counts[0], "ask": counts[1], "deny": counts[2],
        }

    def set_model(self, model: str) -> None:
        self.agent.model = model
        info = self.manager.model_info(model)
        self.agent.context_length = info.context_length if info else None
        # Re-rendered from the *frozen* section bodies, not from a fresh read of
        # the settings files: switching model already rebuilds the prompt cache,
        # but it must not silently apply prompt edits made since the session
        # opened.
        self.agent.history.set_system_prompt(
            render_system_prompt(
                self.manager.env,
                model=model,
                provider=self.manager.provider_name,
                headless=False,
                plan=(self.agent.mode == Mode.plan),
                orchestration=bool(self.resolved.spawns),
                overrides=dict(self.resolved.section_bodies),
            )
        )
        self.manager.config.last_model = model
        self.manager.config.save()
        self.store.append_meta(model=model)
        self.emit({"type": "model_changed", "model": model,
                   "context_length": self.agent.context_length})
        # Any id the provider accepts is allowed; the catalog is a convenience,
        # so an unknown one is a note, not a refusal.
        if self.manager.knows_model(model) is False:
            self.emit({
                "type": "system_note",
                "text": f"(model “{model}” is not in the provider catalog — "
                        f"using it as typed; no context length known)",
            })
        self.emit({"type": "system_prompt", "text": self.agent.history.system_prompt})
        self._emit_state()

    # ---- session-scoped composition switching ----

    def switch_blocked_reason(self) -> str:
        """Why a switch cannot be taken right now, or "" when it can."""
        if self.agent.busy:
            return ("the agent is running — a composition switch takes effect at "
                    "a turn boundary, so it is refused rather than queued")
        if self.pending:
            kind = next(iter(self.pending.values())).kind
            return f"a {kind} review is waiting for your answer"
        if self.input_queue:
            n = len(self.input_queue)
            return f"{n} queued message{'s' if n != 1 else ''} still to send"
        return ""

    def switch_composition(self, preset_id: str) -> dict[str, Any]:
        """Move a running session onto another composition, at a turn boundary.

        Re-resolves, rebuilds the tool registry, re-renders the system prompt
        from the *new* bodies, records a ``composition`` meta record and emits a
        transcript marker. The marker is not decoration: read later without it,
        the log would be misleading, because the same conversation genuinely had
        two different agents in it.

        The cache breakpoint moves once and the next turn pays a full uncached
        input. That is the honest cost, and it is paid at a moment the user
        chose.
        """
        blocked = self.switch_blocked_reason()
        if blocked:
            raise SwitchRefused(blocked)

        manager = self.manager
        preset = preset_module.resolve(manager.cwd, preset_id)
        if preset.id == self.preset_id:
            raise SwitchRefused(f"this session already runs “{preset.title}”")

        pool = session_pool(
            manager.cwd, list(manager.registry_factory().tools.values())
        )
        defs = load_defs(manager.cwd)
        limits = runtime_limits(manager.cwd)
        resolved = resolve_composition(
            ORCHESTRATOR_ID,
            pool=pool,
            preset=preset,
            defs=defs,
            cwd=manager.cwd,
            parent=None,
            depth=0,
            max_depth=limits.max_depth,
            resolve_model=manager.resolve_role,
        )
        limits = runtime_limits(settings=resolved.settings)
        if resolved.errors():
            raise SwitchRefused(resolved.refusal())

        before = self.resolved
        previous_id = self.preset_id
        self.resolved = resolved
        self.preset_id = preset.id

        # The tool list the model is about to be told about, built the same way
        # ``open()`` builds it, from one computation.
        registry = ToolRegistry([t for t in pool if t.name in resolved.tools])
        self.agent.registry = registry
        self.agent.permissions.specs = registry.permission_specs()
        self.agent.limits = limits

        # The ceiling is part of the composition, so a switch can lower it under
        # a session already above it. Clamp rather than leave a mode the new
        # composition forbids.
        if MODE_PRIVILEGE[self.agent.mode] > MODE_PRIVILEGE[resolved.ceiling]:
            self.agent.set_mode(resolved.ceiling)
            self.emit({"type": "mode_changed", "mode": resolved.ceiling.value})

        self.agent.history.set_system_prompt(
            render_system_prompt(
                manager.env,
                model=self.agent.model,
                provider=manager.provider_name,
                headless=False,
                plan=(self.agent.mode == Mode.plan),
                orchestration=bool(resolved.spawns),
                overrides=dict(resolved.section_bodies),
            )
        )

        # Everything a later spawn resolves against moves with the session: the
        # pool, the parent composition, the definitions snapshot and the preset.
        deps = self.agent.ctx.extra.get("subagent")
        if deps is not None:
            deps.pool = pool
            deps.tool_pool = pool
            deps.parent = resolved
            deps.defs = defs
            deps.preset = preset
            deps.limits = limits

        self.store.append_meta(
            preset=preset.id, composition=resolved.to_json(),
        )

        gained = [t for t in resolved.tools if t not in before.tools]
        lost = [t for t in before.tools if t not in resolved.tools]
        self.emit({
            "type": "composition_changed",
            "preset": preset.id,
            "title": preset.title,
            "from_preset": previous_id,
            "tools": list(resolved.tools),
            "gained": gained,
            "lost": lost,
            "ceiling": resolved.ceiling.value,
            "spawns": list(resolved.spawns),
            "digest": resolved.digest(),
        }, log_it=True)
        detail = []
        if gained:
            detail.append("+" + ", ".join(gained))
        if lost:
            detail.append("−" + ", ".join(lost))
        self.emit({
            "type": "system_note",
            "text": (f"composition → {preset.title} "
                     f"({len(resolved.tools)} tools, ceiling {resolved.ceiling.value})"
                     + (f" · {' · '.join(detail)}" if detail else "")),
        })
        self.emit({"type": "system_prompt", "text": self.agent.history.system_prompt})
        self._emit_state()
        return {
            "preset": preset.id,
            "title": preset.title,
            "tools": list(resolved.tools),
            "gained": gained,
            "lost": lost,
            "ceiling": resolved.ceiling.value,
            "digest": resolved.digest(),
        }


class ConversationManager:
    """Builds and tracks live conversations for one project directory."""

    def __init__(
        self,
        *,
        cwd: Path,
        config: Config,
        env: Environment,
        provider: Provider,
        allow_yolo: bool = False,
        default_mode: str | None = None,
        registry_factory=None,
    ) -> None:
        self.cwd = cwd
        self.config = config
        self.env = env
        self.provider = provider
        self._allow_yolo_flag = allow_yolo
        self.default_mode = default_mode
        self.conversations: dict[str, Conversation] = {}
        self._models: list[ModelInfo] | None = None
        profile = config.profile
        self.provider_name = (
            "OpenRouter" if "openrouter.ai" in profile.base_url else profile.base_url
        )
        # Injectable so tests and the plugin loader can shape the toolset.
        if registry_factory is None:
            from quickcode.tools.registry import default_registry

            registry_factory = lambda: default_registry()  # noqa: E731
        self.registry_factory = registry_factory
        # Names of connected MCP servers, set by the launcher for display.
        self.mcp_servers: list[str] = []

    @property
    def allow_yolo(self) -> bool:
        """Is yolo mode reachable in this app at all?

        Read live rather than frozen at construction. ``--yolo`` on the command
        line still arms it, but nobody starts a desktop shortcut with a flag,
        so the setting in Settings → General is the way it is actually reached
        -- and a setting that needed a relaunch to take effect would look just
        as broken as the flag it replaces.
        """
        return self._allow_yolo_flag or bool(self.config.allow_yolo)

    # ---- plugin inventory (for the Settings → Plugins page) ----
    def plugin_inventory(self) -> dict[str, Any]:
        tools = []
        for t in self.registry_factory().tools.values():
            tools.append(
                {
                    "name": t.name,
                    "description": (t.description or "").strip().split("\n")[0][:200],
                    "read_only": t.is_read_only,
                    "source": getattr(t, "source", "internal"),
                }
            )
        return {
            "provider": self.config.profile.provider,
            "tools": tools,
            "mcp_servers": list(self.mcp_servers),
        }

    # ---- models ----
    async def models(self, *, refresh: bool = False) -> list[ModelInfo]:
        if self._models is None or refresh:
            try:
                self._models = await self.provider.list_models()
            except Exception:
                self._models = self._models or []
        return self._models

    def adopt_catalog(self, models: list[ModelInfo]) -> None:
        """Take an install-wide catalog fetched elsewhere, and teach every live
        conversation the context length it has been missing.

        The catalog no longer blocks the window (see ProjectHub._warm_catalog),
        so a conversation can open before it arrives. Everything works without
        it except the context meter, which reads `None` until this fills it in.
        """
        if not models:
            return
        self._models = list(models)
        for conv in self.conversations.values():
            info = self.model_info(conv.agent.model)
            if info is None or conv.agent.context_length == info.context_length:
                continue
            conv.agent.context_length = info.context_length
            conv._emit_state()

    def model_info(self, model_id: str) -> ModelInfo | None:
        for m in self._models or []:
            if m.id == model_id:
                return m
        return None

    def knows_model(self, model_id: str) -> bool | None:
        """Whether the catalog lists this id — None while no catalog is loaded."""
        if not self._models:
            return None
        return any(m.id == model_id for m in self._models)

    # ---- conversations ----
    def get(self, conv_id: str) -> Conversation | None:
        return self.conversations.get(conv_id)

    def _resolve_role(self, spec: str) -> str:
        """A model role ("worker", "orchestrator") to a slug; anything else
        passes through, because any id the provider accepts is allowed."""
        if spec in ("worker", "orchestrator"):
            return self.config.profile.resolve(spec)  # type: ignore[arg-type]
        return spec

    def resolve_role(self, spec: str) -> str:
        """The public name for ``_resolve_role``.

        The workbench has to pass the *same* callable the runner passes or model
        policy would be checked against a different set in the preview than at
        spawn, which is exactly the drift a preview exists to rule out.
        """
        return self._resolve_role(spec)

    @staticmethod
    def _frozen_composition(store: SessionStore, resuming: bool) -> Resolved | None:
        """A resumed session's recorded composition, if it has one.

        Sessions written before compositions existed have no such record and
        fall back to re-resolving, which is exactly what they did before --
        including the fallback to ``standard`` when the preset is gone. A
        session that *does* carry one resumes from it and does not re-resolve,
        which is strictly better: deleting a preset no longer degrades a
        conversation already in flight.
        """
        if not resuming:
            return None
        return Resolved.from_json(store.meta().get("composition"))

    def open(self, conv_id: str | None = None) -> Conversation:
        """Create a new conversation, or attach to / resume an existing one."""
        if conv_id and conv_id in self.conversations:
            return self.conversations[conv_id]

        profile = self.config.profile
        store = SessionStore(self.cwd, conv_id)
        resuming = conv_id is not None and store.path.exists()

        board_path = self.cwd / ".quickcode" / "tasks" / store.conv_id / "board.json"
        board = TaskBoard.load(board_path)

        ctx = ToolCtx(
            cwd=self.cwd,
            read_registry=ReadRegistry(),
            shell_name=self.env.shell_name,
            platform=self.env.platform,
            extra={"task_board": board},
        )

        # The preset is the session's plugin composition. A resumed session
        # keeps the one it started with: the conversation was already told
        # which tools it has, and changing them underneath it is a lie.
        preset = preset_module.resolve(
            self.cwd, store.meta().get("preset", "") if resuming else ""
        )

        # The session pool: everything this install has, minus the plugins that
        # are switched off. Distinct from any one agent's grant -- restricting
        # the orchestrator's tools does not restrict the session, and this is
        # the set that says what the session's envelope actually is.
        pool = session_pool(self.cwd, list(self.registry_factory().tools.values()))
        # Snapshotted once: editing an agent definition takes effect in new
        # sessions, consistent with presets.
        defs = load_defs(self.cwd)

        # Read off disk to resolve with, then re-read from the answer: a
        # resumed session runs on the limits recorded in its own composition,
        # so editing max_rounds or the compaction threshold reaches the next
        # session rather than one already in flight.
        limits = runtime_limits(self.cwd)
        resolved = self._frozen_composition(store, resuming) or resolve_composition(
            ORCHESTRATOR_ID,
            pool=pool,
            preset=preset,
            defs=defs,
            cwd=self.cwd,
            parent=None,
            depth=0,
            max_depth=limits.max_depth,
            resolve_model=self._resolve_role,
        )
        limits = runtime_limits(settings=resolved.settings)

        mode_str = (
            self.default_mode
            or preset.default_mode
            or resolved_default_mode(self.cwd, self.config.default_mode)
        )
        try:
            mode = Mode(mode_str)
        except ValueError:
            mode = Mode.ask
        # The permission posture: the active profile's rules ride *on top of*
        # the project's own (never instead of them, or picking a profile would
        # revoke every "always allow" the user has accrued here), and its mode
        # is where the session starts.
        #
        # Applied identically whether or not this is a resume, which is a
        # decision and not an oversight. The tempting rule -- a profile's mode
        # is a *starting* mode, and a resumed session has already started, so it
        # should keep the mode it had -- has nothing to keep: no per-session
        # mode is ever written to disk, and the ``mode`` computed above is the
        # composition's default re-derived, not this session's. Skipping the
        # profile on resume would preserve nothing; it would swap the posture
        # the user picked for the install default, and for every profile that
        # narrows -- Read only starts in ``plan`` -- that is a resume coming
        # back *wider* than the session it resumes.
        #
        # It is also the reading ``apply_posture`` already commits to: nothing
        # the model has been told depends on the posture, which is the whole
        # reason one can be swapped under a live turn. A thing that may change
        # mid-turn does not need protecting across a reopen.
        posture_mode, rules, posture = effective_posture(
            self.cwd, Rules.load(self.cwd), fallback=mode,
        )
        # Above the preset's ``default_mode``, below ``--mode``. A profile is a
        # file and the flag is the operator saying it at launch, which is the
        # same order ``mode_str`` above already puts them in; the rules apply
        # either way, since the flag has nothing to say about those.
        if not self.default_mode:
            mode = posture_mode
        # The starting mode may not begin above the ceiling. It stays live
        # below it -- rules decide this call, the ceiling decides what is ever
        # possible, and only the second is composition.
        mode = narrower_mode(mode, resolved.ceiling)
        # One registry for the session: the agent runs it, the permission
        # engine reads its tools' declared shapes, and subagents select from it.
        # Built from the resolved tool list, so the answer the UI gives and the
        # tools the model is handed come from one computation.
        registry = ToolRegistry([t for t in pool if t.name in resolved.tools])
        permissions = PermissionEngine(
            mode=mode, rules=rules, root=self.cwd,
            yolo_accepted=self.allow_yolo,
            specs=registry.permission_specs(),
        )

        model = self.config.last_model or profile.resolve("orchestrator")
        info = self.model_info(model)

        history = History(
            render_system_prompt(
                self.env,
                model=model,
                provider=self.provider_name,
                headless=False,
                plan=(mode == Mode.plan),
                orchestration=bool(resolved.spawns),
                overrides=dict(resolved.section_bodies),
            )
        )
        if resuming:
            history.messages = store.load_messages()

        agent = AgentInstance(
            name="main",
            provider=self.provider,
            registry=registry,
            history=history,
            ctx=ctx,
            permissions=permissions,
            model=model,
            permission_cb=None,  # wired below
            context_length=info.context_length if info else None,
            limits=limits,
        )

        # The user's generation settings, applied as each session opens so a
        # change reaches the next one without a restart. The response budget in
        # particular is not a preference: the provider reserves credit against
        # it, and a balance too small for it is refused outright.
        agent.max_tokens = self.config.max_tokens
        agent.temperature = self.config.temperature

        if resuming:
            # Spend belongs to the session, not to this process: restore it
            # from the log so a reopened conversation does not claim it cost
            # nothing so far.
            agent.ledger = Ledger.from_events(store.load_events())

        conv = Conversation(
            conv_id=store.conv_id, agent=agent, store=store, board=board,
            manager=self, resolved=resolved, preset_id=preset.id,
            profile_id=posture.id if posture else "",
        )
        agent.permission_cb = conv.permission_cb
        agent.plan_cb = conv.plan_cb
        ctx.extra["subagent"] = SubagentDeps(
            provider=self.provider,
            profile=profile,
            env=self.env,
            mode_getter=lambda: permissions.mode,
            cwd=self.cwd,
            depth=0,
            on_pane=conv.on_subagent,
            on_done=conv.on_subagent_done,
            # What makes a detached job survive the turn that started it, and
            # what lets interrupt and close reach it afterwards.
            adopt_task=conv.adopt_job,
            owner=agent,
            # The depth-0 carve-out. Children at depth 0 are intersected
            # against the session POOL, not the orchestrator's GRANT: the
            # orchestrator's restriction says what it does with its own hands,
            # not what the session may do. Passing the filtered registry here
            # is what made "delegate everything" hand every subagent an empty
            # toolset. Deeper levels keep intersecting against the parent's
            # grant, which is what ``deps.child()`` passes down.
            pool=pool,
            tool_pool=pool,
            parent=resolved,
            defs=defs,
            preset=preset,
            limits=limits,
        )
        if not resuming:
            store.append_meta(
                title="", model=model, cwd=str(self.cwd), preset=preset.id,
                composition=resolved.to_json(),
            )
        # Log the rendered system prompt (again on resume — a new run may have
        # re-rendered it): the trace must show everything the model sees.
        conv.emit({"type": "system_prompt", "text": history.system_prompt})
        conv.start()
        self.conversations[store.conv_id] = conv
        return conv

    def live_conversations(self) -> dict[str, str]:
        """Open conversations that are genuinely in use, id -> why."""
        out = {}
        for conv_id, conv in self.conversations.items():
            reason = conv.busy_reason()
            if reason:
                out[conv_id] = reason
        return out

    async def release(self, conv_id: str) -> str | None:
        """Close and forget an idle conversation. Returns why not, or None.

        This is what makes "delete this session" work on a session the user
        opened earlier in the same run: it is not in use, so it is closed
        first rather than being refused for the rest of the process's life.
        """
        conv = self.conversations.get(conv_id)
        if conv is None:
            return None
        reason = conv.busy_reason()
        if reason:
            return reason
        await conv.close()
        self.conversations.pop(conv_id, None)
        return None

    async def close(self) -> None:
        await asyncio.gather(
            *(c.close() for c in self.conversations.values()), return_exceptions=True
        )
