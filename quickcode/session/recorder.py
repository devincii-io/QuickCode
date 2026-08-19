"""TranscriptRecorder: the one definition of what a session log looks like.

An ``AgentInstance`` fans its events out on a bus. This turns that stream into
the append-only records a session log is made of -- streamed deltas assembled
into whole assistant messages, tool calls and their results, usage, context
injections, subagent activity (including what each child spent) -- stamping
each with ``turn``/``seq`` and appending it whenever ``loggable()`` accepts it.
It also persists the model-context messages that a resume reads back, and rolls
subagent usage into the session ledger as it goes.

Both callers drive this same object. The server's ``Conversation`` adds the
live fan-out to attached WebSockets and the UI state it keeps in step; the
headless CLI (``quickcode -p``) broadcasts nowhere and adds nothing. A second,
headless-only serializer is exactly what left ``-p`` sessions holding nothing
but their ``meta`` line, so there is deliberately only one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from quickcode.core.compact import run_compaction, should_compact
from quickcode.core.events import (
    AgentStatus,
    ReasoningDelta,
    TextDelta,
    ToolCallEnd,
    ToolResultEvent,
    TurnDone,
    Usage,
)
from quickcode.providers.base import ProviderError
from quickcode.server.serialization import event_to_json, loggable

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance, EventBus, Ledger
    from quickcode.session.store import SessionStore

Handler = Callable[[Any], None]


class TranscriptRecorder:
    """Bus events → session-log records, plus message persistence.

    ``broadcast`` (optional) receives every event *after* it has been stamped,
    which is what the web path pushes to its clients. ``on_usage`` and
    ``on_tasks_changed`` are the two live-only side effects the UI needs; a
    headless run leaves both unset and the log comes out the same.

    ``ledger`` is the session ledger subagent spend is rolled into. The web
    path hands it in at construction; the headless CLI builds the recorder
    before it has an agent, so ``record_turn`` adopts the agent's ledger there.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
        on_usage: Callable[[], None] | None = None,
        on_tasks_changed: Callable[[], None] | None = None,
        persisted: int = 0,
        ledger: Ledger | None = None,
    ) -> None:
        self.store = store
        self.broadcast = broadcast
        self.on_usage = on_usage
        self.on_tasks_changed = on_tasks_changed
        self.ledger = ledger
        # How many of the agent's history messages are already on disk.
        self.persisted = persisted
        self.turn = 0
        # Streaming accumulators for the main agent (assembled on flush).
        self.acc_text: list[str] = []
        self.acc_reasoning: list[str] = []
        self.child_pumps: dict[str, asyncio.Task] = {}
        # The same (queue, handler) pairs as ``_queues``, indexed by agent so
        # one child can be drained on its own -- see ``on_subagent_done``.
        self.child_queues: dict[str, tuple[asyncio.Queue, Handler]] = {}
        self._queues: list[tuple[asyncio.Queue, Handler]] = []

    # ---- the log ----
    def emit(self, ev: dict[str, Any], *, log_it: bool | None = None) -> dict[str, Any]:
        """Persist an event when it is loggable, then hand it to the fan-out."""
        if ev.get("type") == "user_message":
            self.turn += 1
        if log_it if log_it is not None else loggable(ev):
            ev = dict(ev)
            ev.setdefault("turn", self.turn)
            ev["seq"] = self.store.append_event(ev)
        if self.broadcast is not None:
            self.broadcast(ev)
        return ev

    def flush_assistant(self, finish: str) -> None:
        """Emit whatever text/reasoning has streamed in as one whole message."""
        text = "".join(self.acc_text)
        reasoning = "".join(self.acc_reasoning)
        self.acc_text.clear()
        self.acc_reasoning.clear()
        if text or reasoning:
            self.emit(
                {
                    "type": "assistant_message",
                    "text": text,
                    "reasoning": reasoning,
                    "finish_reason": finish,
                }
            )

    def persist_new_messages(self, agent: AgentInstance) -> None:
        """Append the model-context messages this turn added (what resume reads)."""
        msgs = agent.history.messages
        for m in msgs[self.persisted:]:
            self.store.append_message(m)
        self.persisted = len(msgs)

    # ---- main-agent bus ----
    def handle(self, ev: Any) -> None:
        wire = event_to_json(ev)
        if wire is None:
            return
        if isinstance(ev, TextDelta):
            self.acc_text.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            self.acc_reasoning.append(ev.text)
        elif isinstance(ev, ToolCallEnd):
            # Assembled tool call: flush any streamed text first so transcript
            # order (text → tool call) survives replay.
            self.flush_assistant(finish="tool_calls")
            self.emit(wire)
            return
        elif isinstance(ev, ToolResultEvent):
            self.emit(wire)
            if ev.ui_meta.get("tasks_changed") and self.on_tasks_changed is not None:
                self.on_tasks_changed()
            return
        elif isinstance(ev, Usage):
            self.emit(wire)
            if self.on_usage is not None:
                self.on_usage()
            return
        elif isinstance(ev, TurnDone):
            self.flush_assistant(finish=ev.finish_reason)
            if ev.error:
                self.emit({"type": "error", "message": ev.error})
            return
        elif isinstance(ev, AgentStatus):
            if ev.state == "interrupted":
                self.flush_assistant(finish="interrupted")
                self.emit({"type": "system_note", "text": "(interrupted)"})
            self.emit(wire, log_it=False)
            return
        # Fallthrough (deltas, context injections): loggable() decides — deltas
        # stay live-only, context_injection is persisted for the trace.
        self.emit(wire)

    # ---- pumps ----
    def subscribe(self, bus: EventBus, handler: Handler, *, maxsize: int | None = 0):
        """Attach to a bus *now*, before anything can emit onto it.

        Subscribing inside the pump coroutine would lose every event emitted
        before that task first gets scheduled — which is precisely the window a
        headless turn opens in.
        """
        q = bus.subscribe(maxsize=maxsize)
        self._queues.append((q, handler))
        return q

    async def _consume(self, q: asyncio.Queue, handler: Handler) -> None:
        while True:
            handler(await q.get())

    async def pump(self, bus: EventBus) -> None:
        """Consume the main agent's bus (unbounded: the sole consumer)."""
        await self._consume(self.subscribe(bus, self.handle), self.handle)

    def drain(self) -> None:
        """Handle everything still queued on every bus we attached to.

        The pumps are tasks; a turn that ends (or is interrupted) can leave
        events sitting in their queues. Draining synchronously means the log on
        disk is complete by the time the process is free to exit.
        """
        for q, handler in self._queues:
            while True:
                try:
                    ev = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                handler(ev)

    # ---- subagent bridging ----
    def on_subagent(self, agent_id: str, definition: str, bus: EventBus) -> None:
        self.emit({"type": "agent_spawned", "agent_id": agent_id, "definition": definition})
        handler = self._child_handler(agent_id)
        q = self.subscribe(bus, handler, maxsize=None)
        self.child_queues[agent_id] = (q, handler)
        self.child_pumps[agent_id] = asyncio.create_task(self._consume(q, handler))

    def on_subagent_done(
        self, agent_id: str, definition: str, status: str, seconds: float = 0.0
    ) -> None:
        """The closing bracket of ``on_subagent`` — every child gets one.

        A detached job finishes at a moment nothing else in the log marks, so
        without this a reader sees a subagent start and then simply stop having
        events. A blocking one does land its report in the transcript as the
        spawner's tool result, which is why this used to be a detached-only
        record -- but that only serves a reader following the *spawner*.
        Anything following the child (the roster, a replay) was left inferring
        the ending from the child's last ``assistant_message``, and a child
        emits one of those per round, not per turn. So the bracket closes for
        both shapes now, and nobody has to guess.

        A resumed agent gets a second one, correctly: it went terminal twice.

        The child's own queue is drained first. Its events reach the log
        through a pump *task*, while this is called straight from the coroutine
        that was running it -- so without the drain the closing bracket
        overtakes the final message it is supposed to close, and a reader
        walking the log in order watches a finished agent start talking again.
        The child has already stopped emitting by now (its turn returned), so
        this drain is complete, not merely a head start.
        """
        pair = self.child_queues.get(agent_id)
        if pair is not None:
            q, handler = pair
            while True:
                try:
                    handler(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
        self.emit({
            "type": "agent_done",
            "agent_id": agent_id,
            "definition": definition,
            "status": status,
            "seconds": seconds,
        })

    def _child_handler(self, agent_id: str) -> Handler:
        acc_text: list[str] = []

        def handle_child(ev: Any) -> None:
            wire = event_to_json(ev)
            if wire is None:
                return
            if isinstance(ev, TextDelta):
                acc_text.append(ev.text)
            # A child's usage is logged like its calls and results: a subagent
            # owns its own ``Ledger``, so its tokens reach this session only
            # here, and a fan-out that is not written down replays as free.
            logged = isinstance(ev, (ToolCallEnd, ToolResultEvent, Usage))
            if isinstance(ev, TurnDone) and acc_text:
                self.emit(
                    {
                        "type": "agent_event",
                        "agent_id": agent_id,
                        "ev": {
                            "type": "assistant_message",
                            "text": "".join(acc_text),
                            "reasoning": "",
                            "finish_reason": ev.finish_reason,
                        },
                    },
                    log_it=True,
                )
                acc_text.clear()
            self.emit(
                {"type": "agent_event", "agent_id": agent_id, "ev": wire},
                log_it=logged,
            )
            if isinstance(ev, Usage):
                # Cumulative session spend, never the parent's context
                # footprint — see ``Ledger.add_subagent``. Emitted first so a
                # client reads the child's usage before the state event that
                # already counts it.
                if self.ledger is not None:
                    self.ledger.add_subagent(ev)
                if self.on_usage is not None:
                    self.on_usage()

        return handle_child

    # ---- compaction (the headless driver's half of it) ----
    async def maybe_compact(self, agent: AgentInstance) -> bool:
        """Compact when the turn just ended above the declared threshold.

        The web path runs this check in its worker after every turn; ``-p`` had
        no equivalent at all, so a long headless run simply grew until the
        provider refused it. Same function, same setting, same fix-up of
        ``persisted`` — the one thing that differs is that the web path also
        owns the manual ``/compact`` entry point and the live "compacting"
        status event, which a headless run has nobody to show.

        ``runtime.compaction.enabled`` gates the automatic path only.
        """
        limits = agent.limits
        if not limits.compaction_enabled:
            return False
        if not should_compact(agent, limits.compaction_threshold):
            return False
        try:
            summary = await run_compaction(agent, keep_turns=limits.keep_turns)
        except ProviderError as e:
            self.emit({"type": "error", "message": f"compaction failed: {e}"})
            return False
        # History was rebuilt wholesale; without this the summary seed and the
        # kept tail would be appended a second time on the next persist.
        # The rebuilt history goes into the log as well, or the work is
        # undone by the next resume: `load_messages` would replay every
        # original message and hand the model exactly the context compaction
        # existed to remove.
        self.store.append_compaction(agent.history.messages)
        self.persisted = len(agent.history.messages)
        self.emit({"type": "compacted", "summary_chars": len(summary), "manual": False})
        self.emit(
            {"type": "system_note", "text": "(conversation compacted — earlier turns summarized)"}
        )
        return True

    # ---- one recorded turn (the headless driver) ----
    async def record_turn(self, agent: AgentInstance, text: str) -> str:
        """Run one turn of ``agent`` with its whole trace persisted.

        The web path spreads this over a long-lived worker; a ``-p`` run is one
        turn and then the process is gone, so it is written out here. The
        ``finally`` is the point of the shape: an interrupt or a raised turn
        still cancels the pumps, drains what they had not read, flushes any
        half-streamed assistant text and persists the messages — so the log is
        parseable and the session is resumable either way.

        A turn that lands over the compaction threshold is compacted here, the
        way the web worker compacts one: ``-p`` sessions resume, so "one turn
        and then the process is gone" is true of the process, never of the
        conversation.
        """
        # Where a headless recorder meets its ledger: the CLI has to build the
        # recorder before the agent exists, so subagent spend would land
        # nowhere if this waited for the constructor.
        if self.ledger is None:
            self.ledger = agent.ledger
        q = self.subscribe(agent.bus, self.handle)
        pump = asyncio.create_task(self._consume(q, self.handle))
        self.emit({"type": "user_message", "text": text})
        note: dict[str, Any] | None = None
        try:
            out = await agent.run_turn(text)
        except asyncio.CancelledError:
            note = {"type": "system_note", "text": "(interrupted)"}
            raise
        except Exception as e:
            note = {"type": "error", "message": f"{type(e).__name__}: {e}"}
            raise
        finally:
            pump.cancel()
            for t in self.child_pumps.values():
                t.cancel()
            self.drain()
            # Normally a no-op: the provider's TurnDone already flushed. It is
            # not a no-op when the turn was cut short mid-stream, and that text
            # is the only record of what the model had already said.
            self.flush_assistant(finish="interrupted" if agent.cancelled else "stop")
            # After the flush, so the note lands where a reader expects it: at
            # the end of the transcript, not in front of the text it cut off.
            if note is not None:
                self.emit(note)
            self.persist_new_messages(agent)
        # After the messages are on disk (compaction replaces them in memory)
        # and outside the ``finally``, so a turn that raised propagates its own
        # failure instead of waiting on a summarization request.
        await self.maybe_compact(agent)
        return out
