"""TranscriptRecorder: the one definition of what a session log looks like.

An ``AgentInstance`` fans its events out on a bus. This turns that stream into
the append-only records a session log is made of -- streamed deltas assembled
into whole assistant messages, tool calls and their results, usage, context
injections, subagent activity -- stamping each with ``turn``/``seq`` and
appending it whenever ``loggable()`` accepts it. It also persists the
model-context messages that a resume reads back.

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

from quickcode.core.events import (
    AgentStatus,
    ReasoningDelta,
    TextDelta,
    ToolCallEnd,
    ToolResultEvent,
    TurnDone,
    Usage,
)
from quickcode.server.serialization import event_to_json, loggable

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance, EventBus
    from quickcode.session.store import SessionStore

Handler = Callable[[Any], None]


class TranscriptRecorder:
    """Bus events → session-log records, plus message persistence.

    ``broadcast`` (optional) receives every event *after* it has been stamped,
    which is what the web path pushes to its clients. ``on_usage`` and
    ``on_tasks_changed`` are the two live-only side effects the UI needs; a
    headless run leaves both unset and the log comes out the same.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        broadcast: Callable[[dict[str, Any]], None] | None = None,
        on_usage: Callable[[], None] | None = None,
        on_tasks_changed: Callable[[], None] | None = None,
        persisted: int = 0,
    ) -> None:
        self.store = store
        self.broadcast = broadcast
        self.on_usage = on_usage
        self.on_tasks_changed = on_tasks_changed
        # How many of the agent's history messages are already on disk.
        self.persisted = persisted
        self.turn = 0
        # Streaming accumulators for the main agent (assembled on flush).
        self.acc_text: list[str] = []
        self.acc_reasoning: list[str] = []
        self.child_pumps: dict[str, asyncio.Task] = {}
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
        self.child_pumps[agent_id] = asyncio.create_task(self._consume(q, handler))

    def _child_handler(self, agent_id: str) -> Handler:
        acc_text: list[str] = []

        def handle_child(ev: Any) -> None:
            wire = event_to_json(ev)
            if wire is None:
                return
            if isinstance(ev, TextDelta):
                acc_text.append(ev.text)
            logged = isinstance(ev, (ToolCallEnd, ToolResultEvent))
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

        return handle_child

    # ---- one recorded turn (the headless driver) ----
    async def record_turn(self, agent: AgentInstance, text: str) -> str:
        """Run one turn of ``agent`` with its whole trace persisted.

        The web path spreads this over a long-lived worker; a ``-p`` run is one
        turn and then the process is gone, so it is written out here. The
        ``finally`` is the point of the shape: an interrupt or a raised turn
        still cancels the pumps, drains what they had not read, flushes any
        half-streamed assistant text and persists the messages — so the log is
        parseable and the session is resumable either way.
        """
        q = self.subscribe(agent.bus, self.handle)
        pump = asyncio.create_task(self._consume(q, self.handle))
        self.emit({"type": "user_message", "text": text})
        note: dict[str, Any] | None = None
        try:
            return await agent.run_turn(text)
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
