"""A live conversation: one agent behind the web API, and whoever is watching.

One ``Conversation`` per conversation id. It owns the ``AgentInstance``, an
input queue (submits while busy are queued, parity with the old TUI), the
WebSocket attachments, and the append-only trace log. Every bus event is
broadcast live; assembled events (whole messages, tool calls, results,
decisions) are also persisted so replay reconstructs the identical transcript.

Permission and plan review round-trip over the WebSocket; the requests in
flight are kept by the conversation's ``ReviewDesk`` (``server/reviews.py``).

``ConversationManager`` (``server/manager.py``) opens these; this module is
what one of them does once it is open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from quickcode.core.agent import (
    AgentInstance,
    EventBus,
    PermissionOutcome,
    PermissionRequest,
    PlanOutcome,
)
from quickcode.core.compact import run_compaction, should_compact
from quickcode.core.permissions import Mode, Rules
from quickcode.core.profiles import PermissionProfile
from quickcode.core.tasks import TaskBoard
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import MODE_PRIVILEGE, Resolved, narrower_mode
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.resolve import runtime_limits
from quickcode.providers.base import ProviderError
from quickcode.server.reviews import PendingReview, ReviewDesk
from quickcode.session import assemble
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.store import SessionStore
from quickcode.subagents.definitions import load_defs

if TYPE_CHECKING:
    from quickcode.server.manager import ConversationManager

log = logging.getLogger("quickcode.server")

CLIENT_QUEUE_MAX = 4096

# Queued on a conversation's inbox in place of a message: run /compact there.
_COMPACT: Any = object()


class SwitchRefused(Exception):
    """A composition switch that must not happen, carrying why.

    The frozen-composition invariant exists for two reasons and neither is "the
    composition may never change": the model has been told what tools it has,
    and the prompt cache breakpoint sits on the system message. Both survive a
    switch taken *between* turns. So a switch mid-turn is refused -- not queued,
    not applied on the next idle. A switch that lands invisibly three seconds
    later is worse than one that does not happen.
    """


class Client:
    """One attached WebSocket, fed through a bounded queue so a slow reader
    can't block the agent (overflow drops the client; it reconnects and
    replays from the log)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(CLIENT_QUEUE_MAX)
        self.overflowed = False

    def send(self, text: str) -> None:
        if self.overflowed:
            return  # it is about to replay from the log; a gap here is noise
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.overflowed = True
            # The sentinel needs room, and a full queue has none. What is
            # queued is about to be replayed from the log anyway.
            while not self.queue.empty():
                self.queue.get_nowait()
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
        # Whether the current rendering of the system prompt has been
        # shown. See ``emit_system_prompt``.
        self._prompt_shown = False
        self.clients: set[Client] = set()
        self.reviews = ReviewDesk(emit=self.emit, on_plan_resolved=self._emit_state)
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

    @property
    def pending(self) -> dict[str, PendingReview]:
        """Reviews waiting on a client decision, by request id."""
        return self.reviews.pending

    def emit_system_prompt(self) -> None:
        """Show the model's instructions, once per rendering of them.

        Not emitted when the conversation opens. A window that is merely open
        has not sent anything, so a prompt logged there dates the session to
        the moment the app started and leaves a stretch of idle in front of the
        first thing the user did -- which is what the trajectory was drawing.
        It is also not yet final: switching profile or composition before
        typing re-renders it, so an early copy can be one the model never saw.

        Emitted instead at the top of the first turn, and again by anything
        that re-renders it, which is exactly when it becomes true.
        """
        self._prompt_shown = True
        self.emit({"type": "system_prompt", "text": self.agent.history.system_prompt})

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
        # A shell job is a process, not a task: nothing above reaches it, and
        # it would outlive the conversation, the project and the app itself.
        shell_jobs = self.bash_jobs()
        if shell_jobs is not None:
            await asyncio.to_thread(shell_jobs.close)

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

    # ---- background shell jobs ----
    def bash_jobs(self):
        return self.agent.ctx.extra.get("bash_jobs") if self.agent.ctx else None

    def on_bash_job(self, ev: dict[str, Any]) -> None:
        """A background shell job started, wrote output or ended; called on the
        loop thread. ``bash_job_output`` is live-only (not a logged type).

        The transcript gets a note when a job ends on its own, and when a
        person killed it from the Jobs tab -- nothing else in the transcript
        says so. The model's own kill already has a ``bash_kill`` result, and
        the conversation closing is a moment nobody is reading.
        """
        self.emit(ev)
        if ev.get("type") != "bash_job_done":
            return
        if ev.get("status") == "exited":
            text = (f"(background job {ev.get('job_id')} exited with code "
                    f"{ev.get('exit_code')} after {ev.get('seconds')}s)")
        elif ev.get("killed_by") == "user":
            text = (f"(background job {ev.get('job_id')} was killed from the Jobs tab "
                    f"after {ev.get('seconds')}s)")
        else:
            return
        self.emit({"type": "system_note", "text": text})

    def _queue_bash_notices(self) -> None:
        """Tell the model about job endings it has not seen, as a turn starts.

        Decided then rather than when the job ends: until the next turn begins
        the model -- or a subagent sharing the table -- may still read the
        ending itself, and a reminder about something already read is noise.
        """
        jobs = self.bash_jobs()
        if jobs is None:
            return
        for text in jobs.exit_notices():
            self.agent.queue_reminder(text)

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
                "cache_write_tokens": a.ledger.cache_write_tokens,
                "cost_usd": a.ledger.cost_usd,
                # The part of the four numbers above that subagents spent —
                # already included in them, reported separately so the Usage
                # panel can say what a fan-out cost without re-deriving it.
                "subagent_input_tokens": a.ledger.subagent_input_tokens,
                "subagent_output_tokens": a.ledger.subagent_output_tokens,
                "subagent_cost_usd": a.ledger.subagent_cost_usd,
            },
            "pending": self.reviews.listing(),
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
        """Deny every review still waiting on the user; see ``ReviewDesk.deny_all``."""
        return self.reviews.deny_all()

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
            if text is _COMPACT:
                await self._manual_compact()
                continue
            # Ahead of the message, so the trace reads in the order the model
            # sees it: instructions first, then what it was asked.
            if not self._prompt_shown:
                self.emit_system_prompt()
            self.emit({"type": "user_message", "text": text})
            self._queue_bash_notices()
            failure: Exception | None = None
            try:
                # Busy from here, not from inside ``run_turn``: this state
                # event is what shows the Stop button, and the next one may be
                # a whole round away.
                self.agent.busy = True
                self._emit_state()
                await self.agent.run_turn(text)
            except Exception as e:  # never kill the worker
                log.exception("turn failed")
                failure = e
            finally:
                self.agent.busy = False
            # The pump is a task, so the turn's last events can still be queued
            # here. Handled now, before anything is said *about* the turn, or
            # the log records the error ahead of what led to it -- and text
            # that streamed before a raise, which no TurnDone will ever flush,
            # is glued onto the front of the next turn's answer.
            self.rec.drain()
            self.rec.flush_assistant(
                finish="error" if failure else "interrupted" if self.agent.cancelled else "stop"
            )
            if failure is not None:
                self.emit({"type": "error", "message": f"{type(failure).__name__}: {failure}"})
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
        finally:
            # The summary request's usage is logged ahead of ``compacted``, so
            # a replayed ledger counts its spend and then resets the context
            # footprint -- in the order the live one did.
            self.rec.drain()
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
        # Through the worker, like a message: compaction rebuilds the history
        # wholesale, so a turn must not start while the summary is still being
        # written, and closing the conversation must stop it.
        self._inbox.put_nowait(_COMPACT)

    async def _manual_compact(self) -> None:
        try:
            await self._compact(manual=True)
        except Exception as e:  # never kill the worker
            log.exception("compaction failed")
            self.emit({"type": "error", "message": f"compaction failed: {type(e).__name__}: {e}"})
        finally:
            self._emit_state()

    # ---- reviews (permission + plan), kept by the desk ----
    async def permission_cb(self, req: PermissionRequest) -> PermissionOutcome:
        return await self.reviews.permission(req)

    async def plan_cb(self, plan_md: str) -> PlanOutcome:
        return await self.reviews.plan(plan_md)

    def resolve_permission(self, req_id: str, *, allow: bool, persist: bool, deny_message: str) -> bool:
        return self.reviews.resolve_permission(
            req_id, allow=allow, persist=persist, deny_message=deny_message)

    def resolve_plan(self, req_id: str, *, approved: bool, mode_after: str | None, feedback: str) -> bool:
        return self.reviews.resolve_plan(
            req_id, approved=approved, mode_after=mode_after, feedback=feedback)

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
        self.agent.history.set_system_prompt(assemble.system_prompt(
            self.manager.env, self.resolved, model=model,
            provider=self.manager.provider_name, plan=(self.agent.mode == Mode.plan),
        ))
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
        self.emit_system_prompt()
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

        pool = manager.session_pool()
        defs = load_defs(manager.cwd)
        resolved = resolve_orchestrator(
            pool=pool, preset=preset, defs=defs, cwd=manager.cwd,
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
        registry = assemble.session_registry(pool, resolved)
        self.agent.registry = registry
        self.agent.permissions.specs = registry.permission_specs()
        self.agent.limits = limits

        # The ceiling is part of the composition, so a switch can lower it under
        # a session already above it. Clamp rather than leave a mode the new
        # composition forbids.
        if MODE_PRIVILEGE[self.agent.mode] > MODE_PRIVILEGE[resolved.ceiling]:
            self.agent.set_mode(resolved.ceiling)
            self.emit({"type": "mode_changed", "mode": resolved.ceiling.value})

        self.agent.history.set_system_prompt(assemble.system_prompt(
            manager.env, resolved, model=self.agent.model,
            provider=manager.provider_name, plan=(self.agent.mode == Mode.plan),
        ))

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
        self.emit_system_prompt()
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
