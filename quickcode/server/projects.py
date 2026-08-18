"""Multi-project hosting: stable project ids, a persisted registry, a hub of
per-project ``ConversationManager``s, and the directory browser behind them.

One running QuickCode app serves many project directories the way an editor
serves many windows. Everything that is *per project* — the session store, the
permission root, the ``.quickcode/settings.json`` MCP servers, the detected
environment — lives behind its own ``ConversationManager``. Everything that is
*per install* — the provider connection, the model catalog, the loaded tool
plugins — is created once and shared, so opening a second project costs a
directory scan rather than a second catalog fetch.

Project ids are derived from the path rather than allocated, so a URL that
names a project keeps working across restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.config import CONFIG_DIR, Config, Environment
from quickcode.providers.base import ModelInfo, Provider
from quickcode.server.manager import ConversationManager
from quickcode.tools.base import Tool
from quickcode.tools.registry import ToolRegistry, default_registry

log = logging.getLogger("quickcode.server")

REGISTRY_FILENAME = "projects.json"


def project_id(path: str | os.PathLike[str]) -> str:
    """Stable 12-hex id for a project directory.

    Derived, never allocated: the same directory yields the same id in every
    run, so a bookmarked ``#project=<id>`` URL survives a restart. The path is
    normalized to forward slashes first, and case-folded on Windows where
    ``C:\\Proj`` and ``c:\\proj`` name the same directory.
    """
    norm = str(Path(path).resolve()).replace("\\", "/")
    if os.name == "nt":
        norm = norm.casefold()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


@dataclass
class ProjectEntry:
    id: str
    path: Path
    last_opened: str

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)


class ProjectRegistry:
    """The recently-opened project list, persisted at ``~/.quickcode/projects.json``.

    On-disk shape is ``{"projects": [{"path": ..., "last_opened": ...}]}`` —
    ids are recomputed on load so a stale file can never pin a wrong id.
    """

    def __init__(self, path: Path | None = None, *, persist: bool = True) -> None:
        self.path = Path(path) if path is not None else CONFIG_DIR / REGISTRY_FILENAME
        self.persist = persist
        self._entries: dict[str, ProjectEntry] = {}
        if self.persist:
            self._load()

    @classmethod
    def ephemeral(cls) -> ProjectRegistry:
        """An in-memory registry: used when a caller hands us a pre-built
        manager (tests, embedders) and no user state should be touched."""
        return cls(path=None, persist=False)

    # ---- persistence ----
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("projects", []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            p = Path(raw["path"])
            last = raw.get("last_opened")
            entry = ProjectEntry(
                id=project_id(p),
                path=p,
                last_opened=last if isinstance(last, str) else "",
            )
            self._entries[entry.id] = entry

    def save(self) -> None:
        if not self.persist or self.path is None:
            return
        payload = {
            "projects": [
                {"path": str(e.path), "last_opened": e.last_opened}
                for e in self._sorted()
            ]
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save project registry: %s", e)

    # ---- queries ----
    def _sorted(self) -> list[ProjectEntry]:
        return sorted(self._entries.values(), key=lambda e: e.last_opened, reverse=True)

    def touch(self, path: str | os.PathLike[str]) -> ProjectEntry:
        """Record a project as opened now (adding it if new) and persist."""
        p = Path(path).resolve()
        entry = ProjectEntry(
            id=project_id(p),
            path=p,
            last_opened=datetime.datetime.now().isoformat(timespec="seconds"),
        )
        self._entries[entry.id] = entry
        self.save()
        return entry

    def list(self) -> list[ProjectEntry]:
        """Most-recently-opened first, skipping directories that vanished.

        Vanished entries are filtered on read rather than deleted: a project on
        an unmounted drive should come back when the drive does.
        """
        out = []
        for entry in self._sorted():
            try:
                if entry.path.is_dir():
                    out.append(entry)
            except OSError:
                continue
        return out

    def get(self, pid: str) -> ProjectEntry | None:
        return self._entries.get(pid)


async def _no_mcp(cwd) -> tuple[list, list]:
    return [], []


class ProjectHub:
    """Owns one ``ConversationManager`` per open project directory."""

    def __init__(
        self,
        *,
        config: Config,
        provider: Provider,
        allow_yolo: bool = False,
        default_mode: str | None = None,
        registry: ProjectRegistry | None = None,
        plugin_tools: list[Tool] | None = None,
        mcp_connect=None,
        trust_store=None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.allow_yolo = allow_yolo
        self.default_mode = default_mode
        self.registry = registry if registry is not None else ProjectRegistry()
        self.plugin_tools = list(plugin_tools or [])
        # Injectable so tests never touch the real ~/.quickcode/trust.json.
        if trust_store is None:
            from quickcode.security import trust

            trust_store = trust.default_store()
        self._trust_store = trust_store
        if mcp_connect is None:
            from quickcode.plugins import mcp

            mcp_connect = mcp.connect_servers
        self._mcp_connect = mcp_connect
        self.managers: dict[str, ConversationManager] = {}
        self._servers: list[Any] = []
        # Per-project mutable tool list that each manager's registry_factory
        # closes over, so trust granted after open can inject MCP tools live.
        self._project_extra: dict[str, list[Tool]] = {}
        self._models: list[ModelInfo] | None = None
        self._default_id: str | None = None
        self._lock = asyncio.Lock()

    # ---- construction from an existing manager ----
    @classmethod
    def from_manager(cls, manager: ConversationManager) -> ProjectHub:
        """Wrap one pre-built manager so single-project callers (and tests)
        exercise exactly the same routes as the multi-project ones."""
        hub = cls(
            config=manager.config,
            provider=manager.provider,
            allow_yolo=manager.allow_yolo,
            default_mode=manager.default_mode,
            registry=ProjectRegistry.ephemeral(),
            mcp_connect=_no_mcp,
        )
        hub.adopt(manager, default=True)
        return hub

    def adopt(self, manager: ConversationManager, *, default: bool = False) -> str:
        pid = project_id(manager.cwd)
        self.managers[pid] = manager
        self.registry.touch(manager.cwd)
        if default or self._default_id is None:
            self._default_id = pid
        return pid

    # ---- accessors ----
    @property
    def default(self) -> ConversationManager:
        assert self._default_id is not None, "hub has no default project"
        return self.managers[self._default_id]

    @property
    def default_id(self) -> str:
        assert self._default_id is not None, "hub has no default project"
        return self._default_id

    def get(self, pid: str) -> ConversationManager | None:
        return self.managers.get(pid)

    def id_of(self, manager: ConversationManager) -> str:
        return project_id(manager.cwd)

    # ---- opening ----
    async def open(
        self,
        path: str | os.PathLike[str],
        *,
        make_default: bool = False,
        env: Environment | None = None,
    ) -> ConversationManager:
        """Open (or return) the manager for ``path``. Raises ``NotADirectoryError``.

        ``env`` lets the launcher hand over an environment it already detected;
        otherwise the hub detects one itself.
        """
        p = _resolve_dir(path)
        pid = project_id(p)
        async with self._lock:
            manager = self.managers.get(pid)
            if manager is None:
                manager = await self._create(p, env)
                self.managers[pid] = manager
            if make_default or self._default_id is None:
                self._default_id = pid
        self.registry.touch(p)
        return manager

    async def _create(self, path: Path, env: Environment | None = None) -> ConversationManager:
        env = env or Environment.detect(path)
        servers, mcp_tools = await self._mcp_connect(path)
        self._servers.extend(servers)
        pid = project_id(path)
        # Held by reference: registry_factory reads this list every time it runs,
        # so appending to it (grant_trust) makes new conversations see new tools.
        extra = [*self.plugin_tools, *mcp_tools]
        self._project_extra[pid] = extra

        def registry_factory() -> ToolRegistry:
            reg = default_registry()
            for t in extra:
                reg.tools[t.name] = t
            return reg

        manager = ConversationManager(
            cwd=path,
            config=self.config,
            env=env,
            provider=self.provider,
            allow_yolo=self.allow_yolo,
            default_mode=self.default_mode,
            registry_factory=registry_factory,
        )
        manager.mcp_servers = [s.name for s in servers]
        # One catalog per install: the provider is shared, so opening a second
        # project must not pay for a second /models round trip.
        if self._models is None:
            self._models = await manager.models()
        else:
            manager._models = list(self._models)
        return manager

    # ---- trust ----
    def trust_status(self, pid: str) -> dict[str, Any]:
        """The trust decision for an open project: trusted?, which project-scope
        MCP servers exist, and whether any are inert (declared but not started)."""
        manager = self.managers.get(pid)
        if manager is None:
            raise KeyError(pid)
        status = self._trust_store.status(manager.cwd)
        return {**status.to_json(), "running": list(manager.mcp_servers)}

    async def grant_trust(self, pid: str) -> dict[str, Any]:
        """Record trust for an open project and connect its (now-permitted)
        project-scope MCP servers live, so the user need not reopen the project.

        Newly connected tools are appended to the project's shared ``extra``
        list; conversations opened after this see them. Conversations already
        running keep the toolset they started with — restart a session to pick
        up freshly trusted servers.
        """
        from quickcode.plugins import mcp as mcp_module
        from quickcode.security import trust

        manager = self.managers.get(pid)
        if manager is None:
            raise KeyError(pid)
        store = self._trust_store
        store.grant(manager.cwd)

        connected: list[str] = []
        # Only start servers not already tracked, so a repeat grant is a no-op.
        running = set(manager.mcp_servers)
        pending = [n for n in trust.project_mcp_servers(manager.cwd) if n not in running]
        if pending:
            servers, tools = await mcp_module.connect_project_servers(manager.cwd)
            fresh = [s for s in servers if s.name not in running]
            self._servers.extend(fresh)
            extra = self._project_extra.setdefault(pid, [])
            fresh_names = {s.name for s in fresh}
            for t in tools:
                sname = t.name.split("__")[1] if t.name.startswith("mcp__") else ""
                if sname in fresh_names:
                    extra.append(t)
            for s in fresh:
                manager.mcp_servers.append(s.name)
                connected.append(s.name)
        return {**store.status(manager.cwd).to_json(), "connected": connected}

    def revoke_trust(self, pid: str) -> dict[str, Any]:
        """Forget trust for a project. Governs future connects; servers already
        running in this session keep running until the project is torn down."""
        manager = self.managers.get(pid)
        if manager is None:
            raise KeyError(pid)
        existed = self._trust_store.revoke(manager.cwd)
        return {**self._trust_store.status(manager.cwd).to_json(), "revoked": existed}

    # ---- shutdown ----
    async def close(self) -> None:
        await asyncio.gather(
            *(m.close() for m in self.managers.values()), return_exceptions=True
        )
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.stop()
        self._servers.clear()


def _resolve_dir(path: str | os.PathLike[str]) -> Path:
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, ValueError) as e:
        raise NotADirectoryError(str(path)) from e
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    return p


def _is_hidden(entry: os.DirEntry) -> bool:
    """A dot prefix is the POSIX convention; Windows uses a file attribute.

    Honouring it here is what keeps the home directory from listing the
    ACL-denied legacy profile junctions (``Cookies``, ``Anwendungsdaten``, …).
    """
    if os.name != "nt":
        return False
    try:
        return bool(entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
    except (OSError, AttributeError):
        return False


def list_dirs(path: str | None = None) -> dict[str, Any]:
    """Directory-only listing for the project picker.

    Deliberately never reports files: this endpoint is reachable by anything
    holding the loopback token, and a project picker has no business being a
    file browser. Hidden entries are skipped, but a ``.git`` child is still
    consulted so real repos can be highlighted.
    """
    base = _resolve_dir(path) if path else _resolve_dir(Path.home())
    try:
        entries = list(os.scandir(base))
    except OSError as e:
        raise NotADirectoryError(str(base)) from e

    dirs: list[dict[str, Any]] = []
    for entry in entries:
        if entry.name.startswith(".") or _is_hidden(entry):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue  # unreadable junction / reparse point
        child = Path(entry.path)
        try:
            is_git = (child / ".git").exists()
        except OSError:
            is_git = False
        dirs.append({"name": entry.name, "path": str(child), "is_git": is_git})
    dirs.sort(key=lambda d: d["name"].casefold())

    parent = base.parent
    return {
        "path": str(base),
        "parent": str(parent) if parent != base else None,
        "dirs": dirs,
    }
