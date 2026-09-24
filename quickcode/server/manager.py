"""ConversationManager: the live conversations of one project directory.

It opens them -- a new one, or a resume of one on disk -- through
``session/assemble.py``, the same path ``quickcode -p`` takes, then wraps each
in a ``Conversation`` (``server/conversation.py``) and keeps it until it is
released. It also holds what the project's conversations share: the provider,
the model catalog, the tool registry factory and the session pool.

``Client``, ``Conversation``, ``SwitchRefused``, ``PendingReview`` and
``CLIENT_QUEUE_MAX`` are re-exported from here, where callers have always
imported them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from quickcode.config import Config, Environment
from quickcode.kernel.resolve import session_pool as resolve_session_pool
from quickcode.providers.base import ModelInfo, Provider
from quickcode.providers.choice import display_name
from quickcode.server.conversation import (
    CLIENT_QUEUE_MAX,
    Client,
    Conversation,
    SwitchRefused,
)
from quickcode.server.reviews import PendingReview
from quickcode.session import assemble

__all__ = [
    "CLIENT_QUEUE_MAX",
    "Client",
    "Conversation",
    "ConversationManager",
    "PendingReview",
    "SwitchRefused",
]


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
        self.provider_name = display_name(config.profile)
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

    def use_provider(self, provider: Provider) -> None:
        """New conversations talk to ``provider``; open ones keep theirs.

        A conversation already running holds its own reference, so its turn in
        flight and its prompt cache are not pulled out from under it -- the same
        "new sessions pick this up" rule every other install setting follows.
        """
        self.provider = provider
        self.provider_name = display_name(self.config.profile)
        self._models = None

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

    def catalog_size(self) -> int | None:
        """How many models the catalog lists — None while no catalog is loaded."""
        return len(self._models) if self._models else None

    # ---- conversations ----
    def get(self, conv_id: str) -> Conversation | None:
        return self.conversations.get(conv_id)

    def resolve_role(self, spec: str) -> str:
        """A model role ("worker", "orchestrator") to a slug; anything else
        passes through, because any id the provider accepts is allowed.

        The workbench has to pass the *same* callable the runner passes or model
        policy would be checked against a different set in the preview than at
        spawn, which is exactly the drift a preview exists to rule out.
        """
        return assemble.resolve_role(self.config.profile, spec)

    def session_pool(self) -> list[Any]:
        """The tools a session opened now would have: this install's (plugin
        and MCP ones included), plus the project's authored command tools,
        minus every plugin that is switched off. See ``kernel.resolve``."""
        return resolve_session_pool(self.cwd, list(self.registry_factory().tools.values()))

    def open(self, conv_id: str | None = None) -> Conversation:
        """Create a new conversation, or attach to / resume an existing one."""
        if conv_id and conv_id in self.conversations:
            return self.conversations[conv_id]

        session = assemble.build_session(
            self.cwd, self.config, self.env, self.provider,
            pool=self.session_pool(),
            conv_id=conv_id,
            mode=self.default_mode,
            provider_name=self.provider_name,
            yolo_armed=self.allow_yolo,
            model_info=self.model_info,
        )
        agent = session.agent
        conv = Conversation(
            conv_id=session.conv_id, agent=agent, store=session.store,
            board=session.board, manager=self, resolved=session.resolved,
            preset_id=session.preset.id,
            profile_id=session.posture.id if session.posture else "",
        )
        agent.permission_cb = conv.permission_cb
        agent.plan_cb = conv.plan_cb
        session.wire(
            on_pane=conv.on_subagent,
            on_done=conv.on_subagent_done,
            on_bash_event=conv.on_bash_job,
            # What makes a detached job survive the turn that started it, and
            # what lets interrupt and close reach it afterwards.
            adopt_task=conv.adopt_job,
        )
        session.begin_log()
        if session.unarmed_yolo:
            # After ``begin_log``, so it is held behind the opening record like
            # anything else said before the user speaks.
            conv.emit({
                "type": "system_note",
                "text": (f"this session was asked to start in yolo mode: "
                         f"{conv.YOLO_UNARMED}. Started in {agent.mode.value} instead."),
            })
        conv.start()
        self.conversations[session.conv_id] = conv
        return conv

    def live_conversations(self) -> dict[str, str]:
        """Open conversations that are genuinely in use, id -> why."""
        out = {}
        # A snapshot: the session list asks from a worker thread while the
        # event loop may be opening a conversation into this dict.
        for conv_id, conv in list(self.conversations.items()):
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
