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
from quickcode.pty import registry as terminal_registry
from quickcode.server.manager import ConversationManager
from quickcode.session.store import (
    project_data_dir,
    project_data_summary,
    purge_project_data,
)
from quickcode.tools.base import Tool
from quickcode.tools.registry import ToolRegistry, default_registry

log = logging.getLogger("quickcode.server")

REGISTRY_FILENAME = "projects.json"


class ProjectBusyError(RuntimeError):
    """A project cannot be forgotten while conversations are live in it."""

    def __init__(self, pid: str, live: int) -> None:
        self.pid = pid
        self.live = live
        word = "conversation" if live == 1 else "conversations"
        super().__init__(f"{live} live {word} in this project; close them first")


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

    def __init__(self, path: Path | None, *, persist: bool = True) -> None:
        """``path`` is required, and may only be ``None`` for a non-persisting
        registry.

        It used to default to ``CONFIG_DIR / REGISTRY_FILENAME``, which made
        "write to the user's real recent-projects list" the behaviour of the
        shortest possible call. A test suite took that default and put 258
        pytest temp directories in somebody's home screen. Callers that do want
        the user's file now say so out loud: ``ProjectRegistry.user_default()``.
        """
        if persist and path is None:
            raise ValueError(
                "a persisting ProjectRegistry needs a path; "
                "use ProjectRegistry.ephemeral() for one that writes nothing"
            )
        self.path = Path(path) if path is not None else None
        self.persist = persist
        self._entries: dict[str, ProjectEntry] = {}
        if self.persist:
            self._load()

    @classmethod
    def user_default(cls) -> ProjectRegistry:
        """The real ``~/.quickcode/projects.json`` -- the app's own registry.

        ``CONFIG_DIR`` is looked up when this is called, not baked into a
        default argument, so whatever has rebound this module's copy of it is
        honoured -- which is how the test suite keeps out (tests/conftest.py).
        """
        return cls(CONFIG_DIR / REGISTRY_FILENAME)

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

    def remove(self, pid: str) -> ProjectEntry | None:
        """Forget one entry and persist. Returns what was dropped, or ``None``.

        This is a *list* operation and nothing more: it touches
        ``~/.quickcode/projects.json`` and never the project directory. Opening
        the same folder again re-adds it, unchanged, with everything inside it
        exactly as it was.
        """
        entry = self._entries.pop(pid, None)
        if entry is not None:
            self.save()
        return entry


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
        defer_catalog: bool = False,
    ) -> None:
        self.config = config
        self.provider = provider
        self.allow_yolo = allow_yolo
        self.default_mode = default_mode
        # Named, not implied: the fallback writes the user's real recent-projects
        # list, and every caller that is not the app wants `ephemeral()` or a
        # path of its own. Kept as a fallback rather than made required because
        # the app is the overwhelmingly common caller and `from_manager` already
        # passes one -- the guard against tests reaching it lives in conftest.
        self.registry = registry if registry is not None else ProjectRegistry.user_default()
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
        self._models_task: asyncio.Task | None = None
        # The launcher sets this so the first project's catalog fetch waits for
        # the window; every other caller warms as soon as a project opens.
        self._defer_catalog = defer_catalog
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
        if self._models is not None:
            manager._models = list(self._models)
        elif not self._defer_catalog:
            self._warm_catalog(manager)
        return manager

    def warm_catalog(self) -> None:
        """Start the catalog fetch now — the launcher calls this once the port
        is bound and the window is on its way.

        Backgrounding the fetch was not enough on its own: parsing 400 models
        runs on the same event loop, and it was still winning the race against
        uvicorn's bind, which pushed the window out by ~1.5 s. Held until the
        boot path is done, it costs nothing anyone can see.
        """
        self._defer_catalog = False
        if self._models is not None or not self.managers:
            return
        self._warm_catalog(next(iter(self.managers.values())))

    def _warm_catalog(self, manager: ConversationManager) -> None:
        """Fetch the model catalog *beside* startup instead of in front of it.

        Listing the provider's models is a 400-entry HTTP round trip — measured
        at 3.2 s here — and it used to be awaited before uvicorn bound its port,
        so nothing appeared on screen until it came back. Nothing on screen
        needs it: the window, the transcript and the composer all render from
        config. The one thing that does is a model's context length, for the
        context meter, and that can arrive a moment later (`adopt_catalog`).
        A failed fetch leaves `_models` as it was — an unknown catalog, which
        the app already treats as "use the id as typed".
        """
        if self._models_task is not None and not self._models_task.done():
            return  # one fetch per install, already in flight

        async def warm() -> None:
            models = await manager.models()   # never raises; [] on failure
            if not models:
                return
            self._models = models
            for other in self.managers.values():
                other.adopt_catalog(models)

        self._models_task = asyncio.create_task(warm())

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

    # ---- forgetting a project ----
    def data_summary(self, pid: str) -> dict[str, Any]:
        """What ``forget(purge_data=True)`` would remove, for the confirmation.

        Answers for a project that is merely *listed* as well as one that is
        open — the home view offers this on a card that was never expanded.
        """
        return project_data_summary(self.path_of(pid))

    def path_of(self, pid: str) -> Path:
        """The directory a project id names. Raises ``KeyError`` if unknown.

        The registry is asked first, because a project can be on the list
        without a manager; an open project that somehow left the list still
        answers, so no id the app is currently serving can become unaddressable.
        """
        entry = self.registry.get(pid)
        if entry is not None:
            return Path(entry.path)
        manager = self.managers.get(pid)
        if manager is not None:
            return Path(manager.cwd)
        raise KeyError(pid)

    async def forget(self, pid: str, *, purge_data: bool = False) -> dict[str, Any]:
        """Drop a project from the recent list; optionally delete its data too.

        Two different acts behind one door, and the caller says which:

        * ``purge_data=False`` — the registry entry goes and **nothing on disk
          is touched**. This is "remove from the list".
        * ``purge_data=True`` — additionally ``<project>/.quickcode`` is deleted
          and the project's grant is dropped from the trust store. The project
          directory itself, and everything else in it, is never touched; see
          ``session.store.project_data_dir`` for the containment proof.

        A project with live conversations is **refused** (``ProjectBusyError``)
        rather than closed from under them. That is the precedent
        ``DELETE /api/sessions/{conv_id}`` already sets for a live session, and
        the reasons are the same: a live conversation has a websocket, a running
        turn and possibly a tool mid-flight, and pulling its log away is a
        different and worse failure than being told to close it first. Closing
        it silently would also be an unaskable question — "delete" was clicked
        on a list row, not on the conversation.

        A project that is open but *idle* is closed and dropped from the hub, so
        nothing keeps running against a directory the user just took off the
        list. The one exception is the default project: this app process is
        serving it, and ``hub.default`` must keep answering.

        Raises ``KeyError`` (unknown id), ``ProjectBusyError`` (live), or
        ``ValueError`` (a data directory that could not be proved safe — nothing
        is deleted and nothing is closed when that happens).
        """
        path = self.path_of(pid)
        manager = self.managers.get(pid)
        if manager is not None:
            # Conversations opened earlier in this run and since abandoned are
            # not a reason to refuse: nothing is attached and nothing is
            # running. Only the ones actually in use are.
            live = manager.live_conversations()
            if live:
                raise ProjectBusyError(pid, len(live))
        # Prove the target before anything is closed or unlinked, so a refusal
        # leaves the project exactly as it was.
        if purge_data:
            project_data_dir(path)
        result: dict[str, Any] = {
            "id": pid,
            "path": str(path),
            "removed_from_list": False,
            "closed": False,
            "data_dir": None,
            "data_existed": False,
            "data_deleted": False,
            "trust_revoked": False,
        }
        if manager is not None and pid != self._default_id:
            # A terminal panel open on this project holds a login shell that
            # nothing else would ever kill: it is not a conversation, so the
            # liveness check above does not see it, and its socket belongs to a
            # window that is about to be told the project is gone.
            terminal_registry.close_for(path)
            await manager.close()
            self.managers.pop(pid, None)
            self._project_extra.pop(pid, None)
            result["closed"] = True
        if purge_data:
            purge = purge_project_data(path)
            result["data_dir"] = purge.path
            result["data_existed"] = purge.existed
            result["data_deleted"] = purge.removed
            result["trust_revoked"] = self._trust_store.revoke(path)
        result["removed_from_list"] = self.registry.remove(pid) is not None
        return result

    # ---- shutdown ----
    async def close(self) -> None:
        # Terminals first: they are the only children that would survive this
        # process, since a login shell has no parent link back to the server.
        terminal_registry.close_all()
        if self._models_task is not None:
            self._models_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._models_task
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
