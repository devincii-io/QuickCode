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
    PermissionOutcome,
    PermissionRequest,
    PlanOutcome,
)
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
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.core.tasks import TaskBoard
from quickcode.prompts.system import render_system_prompt
from quickcode.providers.base import ModelInfo, Provider, ProviderError
from quickcode.server.serialization import event_to_json, loggable
from quickcode.session.store import SessionStore
from quickcode.subagents.runner import SubagentDeps
from quickcode.tools.base import ReadRegistry, ToolCtx

log = logging.getLogger("quickcode.server")

CLIENT_QUEUE_MAX = 4096


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
    ) -> None:
        self.conv_id = conv_id
        self.agent = agent
        self.store = store
        self.board = board
        self.manager = manager
        self.clients: set[Client] = set()
        self.pending: dict[str, PendingReview] = {}
        self.input_queue: list[str] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._persisted = len(agent.history.messages)
        self._turn = 0
        self._tasks: list[asyncio.Task] = []
        # streaming accumulators for the main agent (assemble log records)
        self._acc_text: list[str] = []
        self._acc_reasoning: list[str] = []
        self._child_pumps: dict[str, asyncio.Task] = {}

    # ---- lifecycle ----
    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._pump(self.agent.bus)))
        self._tasks.append(asyncio.create_task(self._worker()))

    async def close(self) -> None:
        self.agent.cancel()
        for t in [*self._tasks, *self._child_pumps.values()]:
            t.cancel()
        await asyncio.gather(
            *self._tasks, *self._child_pumps.values(), return_exceptions=True
        )

    # ---- event fan-out ----
    def emit(self, ev: dict[str, Any], *, log_it: bool | None = None) -> dict[str, Any]:
        """Broadcast an event to all clients; persist it when loggable."""
        if ev.get("type") == "user_message":
            self._turn += 1
        if log_it if log_it is not None else loggable(ev):
            ev = dict(ev)
            ev.setdefault("turn", self._turn)
            ev["seq"] = self.store.append_event(ev)
        text = json.dumps(ev, ensure_ascii=False)
        for c in list(self.clients):
            c.send(text)
        return ev

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
            },
            "pending": [
                {"req_id": p.req_id, "kind": p.kind, **p.payload}
                for p in self.pending.values()
            ],
            "tasks": [t.to_dict() for t in self.board.list()],
        }

    # ---- main-agent bus pump ----
    async def _pump(self, bus: EventBus) -> None:
        q = bus.subscribe(maxsize=0)  # unbounded: the sole server-side consumer
        while True:
            ev = await q.get()
            self._handle_bus_event(ev)

    def _handle_bus_event(self, ev: Any) -> None:
        wire = event_to_json(ev)
        if wire is None:
            return
        if isinstance(ev, TextDelta):
            self._acc_text.append(ev.text)
        elif isinstance(ev, ReasoningDelta):
            self._acc_reasoning.append(ev.text)
        elif isinstance(ev, ToolCallEnd):
            # Assembled tool call: flush any streamed text first so transcript
            # order (text → tool call) survives replay.
            self._flush_assistant(finish="tool_calls")
            self.emit(wire)
            return
        elif isinstance(ev, ToolResultEvent):
            self.emit(wire)
            if ev.name.startswith("task_"):
                self.emit(
                    {"type": "tasks", "tasks": [t.to_dict() for t in self.board.list()]},
                    log_it=False,
                )
            return
        elif isinstance(ev, Usage):
            self.emit(wire)
            self._emit_state()
            return
        elif isinstance(ev, TurnDone):
            self._flush_assistant(finish=ev.finish_reason)
            if ev.error:
                self.emit({"type": "error", "message": ev.error})
            return
        elif isinstance(ev, AgentStatus):
            if ev.state == "interrupted":
                self._flush_assistant(finish="interrupted")
                self.emit({"type": "system_note", "text": "(interrupted)"})
            self.emit(wire, log_it=False)
            return
        # Fallthrough (deltas, context injections): loggable() decides — deltas
        # stay live-only, context_injection is persisted for the trace.
        self.emit(wire)

    def _flush_assistant(self, finish: str) -> None:
        text = "".join(self._acc_text)
        reasoning = "".join(self._acc_reasoning)
        self._acc_text.clear()
        self._acc_reasoning.clear()
        if text or reasoning:
            self.emit(
                {
                    "type": "assistant_message",
                    "text": text,
                    "reasoning": reasoning,
                    "finish_reason": finish,
                }
            )

    # ---- subagent bridging ----
    def on_subagent(self, agent_id: str, definition: str, bus: EventBus) -> None:
        self.emit({"type": "agent_spawned", "agent_id": agent_id, "definition": definition})
        task = asyncio.create_task(self._pump_child(agent_id, bus))
        self._child_pumps[agent_id] = task

    async def _pump_child(self, agent_id: str, bus: EventBus) -> None:
        q = bus.subscribe()
        acc_text: list[str] = []
        try:
            while True:
                ev = await q.get()
                wire = event_to_json(ev)
                if wire is None:
                    continue
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
        except asyncio.CancelledError:
            raise

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

    def interrupt(self) -> None:
        cleared = len(self.input_queue)
        self.input_queue.clear()
        if self.agent.busy:
            self.agent.cancel()
        note = "(interrupt requested)"
        if cleared:
            note = f"(interrupt requested; {cleared} queued message{'s' if cleared != 1 else ''} cleared)"
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
            self._persist_new_messages()
            if should_compact(self.agent):
                await self._compact(manual=False)
            self._emit_state()
            # Send the next queued follow-up, if any.
            if self.input_queue and not self.agent.busy:
                self._inbox.put_nowait(self.input_queue.pop(0))

    def _persist_new_messages(self) -> None:
        msgs = self.agent.history.messages
        for m in msgs[self._persisted:]:
            self.store.append_message(m)
        self._persisted = len(msgs)

    # ---- compaction ----
    async def _compact(self, *, manual: bool) -> None:
        self.emit({"type": "status", "state": "sending", "detail": "compacting"}, log_it=False)
        try:
            summary = await run_compaction(self.agent)
        except ProviderError as e:
            self.emit({"type": "error", "message": f"compaction failed: {e}"})
            return
        # History was rebuilt wholesale; future messages persist from here.
        self._persisted = len(self.agent.history.messages)
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
    def set_mode(self, mode_str: str) -> None:
        try:
            mode = Mode(mode_str)
        except ValueError:
            self.emit({"type": "error", "message": f"unknown mode: {mode_str}"})
            return
        if mode == Mode.yolo and not self.agent.permissions.yolo_accepted:
            self.emit({"type": "error", "message": "yolo mode requires launching with --yolo"})
            return
        self.agent.set_mode(mode)
        self.emit({"type": "mode_changed", "mode": mode.value})
        self._emit_state()

    def set_model(self, model: str) -> None:
        self.agent.model = model
        info = self.manager.model_info(model)
        self.agent.context_length = info.context_length if info else None
        self.agent.history.set_system_prompt(
            render_system_prompt(
                self.manager.env,
                model=model,
                provider=self.manager.provider_name,
                headless=False,
                plan=(self.agent.mode == Mode.plan),
                orchestration=True,
            )
        )
        self.manager.config.last_model = model
        self.manager.config.save()
        self.store.append_meta(model=model)
        self.emit({"type": "model_changed", "model": model,
                   "context_length": self.agent.context_length})
        self.emit({"type": "system_prompt", "text": self.agent.history.system_prompt})
        self._emit_state()


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
        self.allow_yolo = allow_yolo
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

    # ---- plugin inventory (for the Settings → Plugins page) ----
    def plugin_inventory(self) -> dict[str, Any]:
        from quickcode.tools.registry import default_registry

        builtin = set(default_registry().tools)
        tools = []
        for t in self.registry_factory().tools.values():
            source = "builtin" if t.name in builtin else (
                "mcp" if t.name.startswith("mcp__") else "plugin"
            )
            tools.append(
                {
                    "name": t.name,
                    "description": (t.description or "").strip().split("\n")[0][:200],
                    "read_only": t.is_read_only,
                    "source": source,
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

    def model_info(self, model_id: str) -> ModelInfo | None:
        for m in self._models or []:
            if m.id == model_id:
                return m
        return None

    # ---- conversations ----
    def get(self, conv_id: str) -> Conversation | None:
        return self.conversations.get(conv_id)

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

        mode_str = self.default_mode or self.config.default_mode
        try:
            mode = Mode(mode_str)
        except ValueError:
            mode = Mode.ask
        permissions = PermissionEngine(
            mode=mode, rules=Rules.load(self.cwd), root=self.cwd,
            yolo_accepted=self.allow_yolo,
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
                orchestration=True,
            )
        )
        if resuming:
            history.messages = store.load_messages()

        agent = AgentInstance(
            name="main",
            provider=self.provider,
            registry=self.registry_factory(),
            history=history,
            ctx=ctx,
            permissions=permissions,
            model=model,
            permission_cb=None,  # wired below
            context_length=info.context_length if info else None,
        )

        conv = Conversation(
            conv_id=store.conv_id, agent=agent, store=store, board=board, manager=self
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
        )
        if not resuming:
            store.append_meta(title="", model=model, cwd=str(self.cwd))
        # Log the rendered system prompt (again on resume — a new run may have
        # re-rendered it): the trace must show everything the model sees.
        conv.emit({"type": "system_prompt", "text": history.system_prompt})
        conv.start()
        self.conversations[store.conv_id] = conv
        return conv

    async def close(self) -> None:
        await asyncio.gather(
            *(c.close() for c in self.conversations.values()), return_exceptions=True
        )
