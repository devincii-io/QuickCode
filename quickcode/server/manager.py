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
        # The session log itself — turn/seq stamping, assembly of the streamed
        # events, message persistence. Shared verbatim with the headless CLI so
        # `-p` and the UI cannot write two different kinds of session.
        self.rec = TranscriptRecorder(
            store,
            broadcast=self._broadcast,
            on_usage=self._emit_state,
            on_tasks_changed=self._emit_tasks,
            persisted=len(agent.history.messages),
        )

    # ---- lifecycle ----
    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self.rec.pump(self.agent.bus)))
        self._tasks.append(asyncio.create_task(self._worker()))

    async def close(self) -> None:
        self.agent.cancel()
        children = list(self.rec.child_pumps.values())
        for t in [*self._tasks, *children]:
            t.cancel()
        await asyncio.gather(*self._tasks, *children, return_exceptions=True)

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
            },
            "pending": [
                {"req_id": p.req_id, "kind": p.kind, **p.payload}
                for p in self.pending.values()
            ],
            "tasks": [t.to_dict() for t in self.board.list()],
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
            self.rec.persist_new_messages(self.agent)
            # ``runtime.compaction.enabled`` gates the automatic path only:
            # /compact is a thing the user asked for, and switching the
            # automatic summary off is not a statement about that.
            limits = self.agent.limits
            if limits.compaction_enabled and should_compact(
                self.agent, limits.compaction_threshold
            ):
                await self._compact(manual=False)
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
            mode=mode, rules=Rules.load(self.cwd), root=self.cwd,
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

        if resuming:
            # Spend belongs to the session, not to this process: restore it
            # from the log so a reopened conversation does not claim it cost
            # nothing so far.
            agent.ledger = Ledger.from_events(store.load_events())

        conv = Conversation(
            conv_id=store.conv_id, agent=agent, store=store, board=board,
            manager=self, resolved=resolved, preset_id=preset.id,
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

    async def close(self) -> None:
        await asyncio.gather(
            *(c.close() for c in self.conversations.values()), return_exceptions=True
        )
