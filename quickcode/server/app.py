"""FastAPI app: REST bootstrap/sessions/models/config, WS attach, static frontend.

Local-only trust boundary (same model as QuickTerm): the API answers the
QuickCode window and nothing else. The Host allowlist defeats DNS-rebinding,
the Origin allowlist defeats cross-origin requests from other sites in the
same browser, and the loopback token (server/auth.py) stops other local
processes. Static frontend files carry no secrets and stay open so the shell
can bootstrap.

Routes come in two shapes over the same handlers: the legacy single-project
paths (``/api/bootstrap``, ``/ws/conversation/{id}``…), which address the hub's
default project, and the ``/api/projects/{project_id}/…`` paths, which address
any open project. The default project is just the launch directory, so the two
shapes never diverge for a single-project run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from quickcode.config import _float_or_none, _int_or
from quickcode.kernel import preset as preset_module
from quickcode.kernel.spec import (
    LockedSetting,
    NeedsConfirmation,
    UnknownPlugin,
    UnknownSetting,
)
from quickcode.kernel.state import prompt_overrides
from quickcode.prompts.system import render_with_sections
from quickcode.server import auth
from quickcode.server.agents_api import register_agent_routes
from quickcode.server.authoring_api import register_authoring_routes
from quickcode.server.gitinfo import register_git_routes
from quickcode.server.manager import Client, Conversation, ConversationManager
from quickcode.server.paths import register_path_routes
from quickcode.server.projects import ProjectBusyError, ProjectHub, list_dirs
from quickcode.server.terminal import register_terminal_routes
from quickcode.session.store import (
    MAX_TITLE,
    SESSIONS_DIRNAME,
    SessionStore,
    purge_sessions,
)

log = logging.getLogger("quickcode.server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
JSON_BODY_CAP = 1024 * 1024
# Conversation ids are generated as hex; anything else in a path segment would
# be a traversal attempt against the sessions directory.
_CONV_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# A profile id is a key in a settings file and a path segment in these routes,
# so it is held to the shape both can carry losslessly.
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _valid_conv_id(conv_id: str) -> bool:
    return _CONV_ID_RE.fullmatch(conv_id) is not None


def _rejected_setting(provider: str, key: str, allowed: set[str]) -> str:
    """Why a search setting was refused, in terms of what to do instead."""
    if key == "api_key":
        return (
            "an API key is not saved through this route — POST /api/search-key "
            "puts it in the encrypted store, config.json is plain text"
        )
    return f"{key!r} is not a setting of {provider} (it takes: {', '.join(sorted(allowed))})"


def _session_count(root: Path) -> int:
    sessions_dir = root / SESSIONS_DIRNAME
    try:
        return sum(1 for _ in sessions_dir.glob("*.jsonl"))
    except OSError:
        return 0


def _allowed_origins(host: str, port: int) -> tuple[set[str], set[str]]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::"):
        hosts.add(f"{host}:{port}")
    return hosts, {f"http://{h}" for h in hosts}


def create_app(
    target: ConversationManager | ProjectHub,
    *,
    host: str = "127.0.0.1",
    port: int = 8642,
    token: str = "",
) -> FastAPI:
    # A bare manager is the single-project shape; wrap it so every handler has
    # exactly one code path.
    hub = target if isinstance(target, ProjectHub) else ProjectHub.from_manager(target)
    app = FastAPI(title="QuickCode", docs_url=None, redoc_url=None)
    allowed_hosts, allowed_origins = _allowed_origins(host, port)

    def _project(pid: str) -> ConversationManager:
        manager = hub.get(pid)
        if manager is None:
            raise HTTPException(404, f"unknown project: {pid}")
        return manager

    def _token_required(request: Request) -> bool:
        path = request.url.path
        return path.startswith("/api/") and path != "/api/health"

    @app.middleware("http")
    async def _local_guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return Response("forbidden: bad host", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return Response("forbidden: bad origin", status_code=403)
        if token and _token_required(request) and request.headers.get(auth.HEADER) != token:
            return Response("forbidden: bad token", status_code=403)
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/ws"):
            # Force revalidation for the shell so a stale UI never survives an
            # app update.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def _ws_allowed(ws: WebSocket) -> bool:
        if ws.headers.get("host", "") not in allowed_hosts:
            return False
        origin = ws.headers.get("origin")
        # browsers always send Origin on WS; absent means a native local client
        if not (origin is None or origin in allowed_origins):
            return False
        if token:
            offered = ws.headers.get("sec-websocket-protocol", "")
            wanted = auth.SUBPROTOCOL_PREFIX + token
            if wanted not in [p.strip() for p in offered.split(",")]:
                return False
        return True

    # ---- REST ----

    @app.get("/api/health")
    def health() -> dict:
        from quickcode.cli import __version__

        return {"app": "quickcode", "version": __version__}

    # ---- per-project payload builders (shared by both route shapes) ----

    def _bootstrap(manager: ConversationManager) -> dict:
        from quickcode.cli import __version__
        from quickcode.config import THEME_PRESETS

        cfg = manager.config
        profile = cfg.profile
        return {
            # The presets ride along so Settings can offer them without
            # duplicating eleven hex values per palette in the frontend.
            "theme_presets": THEME_PRESETS,
            "version": __version__,
            "cwd": str(manager.cwd),
            "project": manager.cwd.name,
            "git_branch": manager.env.git_branch,
            "provider": manager.provider_name,
            "base_url": profile.base_url,
            "default_model": cfg.last_model or profile.resolve("orchestrator"),
            "default_mode": manager.default_mode or cfg.default_mode,
            "allow_yolo": manager.allow_yolo,
            "theme": cfg.theme_colors(),
            "has_api_key": bool(profile.api_key),
            "api_key_env": profile.api_key_env,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "search": _search_payload(cfg),
        }

    def _search_payload(cfg) -> dict:
        """Which search backend answers, and what each one still needs.

        Rides along with the bootstrap so Settings can draw the whole page —
        the fields a provider has, its signup page, its free tier — without a
        second round-trip and without the frontend hardcoding six providers.

        No key, and no part of one, is in here. ``configured`` and ``missing``
        are ``configured_providers`` / ``resolve_credentials`` answering the
        same questions ``doctor.check_search`` asks, so the two never disagree;
        everything else is either non-secret by definition (a base URL, an
        engine id) or is a name rather than a value.
        """
        from quickcode.search import (
            KEY_SOURCE_SAVED,
            chosen_provider,
            configured_providers,
            key_source,
            provider_infos,
            resolve_credentials,
        )

        settings = cfg.search
        ready = set(configured_providers(settings))
        providers = []
        for info in provider_infos():
            saved = settings.for_provider(info.name)
            credentials, missing = resolve_credentials(info, settings)
            is_ready = info.name in ready
            source = key_source(info, settings) if info.needs_key and is_ready else ""
            providers.append(
                {
                    "name": info.name,
                    "label": info.label,
                    "configured": is_ready,
                    "missing": missing,
                    "needs_key": info.needs_key,
                    "api_key_env": info.api_key_env,
                    # Where the key is being read from, in doctor's words. The
                    # store is the only source Settings can write, so a key
                    # coming from anywhere else is worth saying out loud.
                    "key_source": source,
                    "key_from_store": source == KEY_SOURCE_SAVED,
                    "needs_base_url": info.needs_base_url,
                    "base_url_env": info.base_url_env,
                    "base_url": saved.get("base_url", ""),
                    "base_url_in_use": credentials.base_url,
                    "extra_fields": [
                        {
                            "key": key,
                            "env": var,
                            "label": label,
                            "value": saved.get(key, ""),
                            "in_use": credentials.extra.get(key, ""),
                        }
                        for key, var, label in info.extra_fields
                    ],
                    "signup_url": info.signup_url,
                    "docs_url": info.docs_url,
                    "free_tier": info.free_tier,
                }
            )
        return {"provider": chosen_provider(settings), "providers": providers}

    def _sessions(manager: ConversationManager, include_archived: bool = False) -> list[dict]:
        # "Live" means in use -- attached, running, or holding a job -- not
        # "was opened at some point in this process". The dot in the UI and
        # the 409 from delete now agree, and both are true.
        live = set(manager.live_conversations())
        out = []
        for info in SessionStore.list_sessions(
            manager.cwd, include_archived=include_archived
        ):
            out.append(
                {
                    "conv_id": info.conv_id,
                    "title": info.title,
                    "model": info.model,
                    "mtime": info.mtime,
                    "message_count": info.message_count,
                    "live": info.conv_id in live,
                    "archived": info.archived,
                }
            )
        return out

    def _revive(manager: ConversationManager, conv_id: str) -> None:
        """Bring an archived session back into the list before opening it.

        Working in a session is the opposite of having filed it away, and a
        live-but-hidden conversation would be the worst of both.
        """
        if _valid_conv_id(conv_id):
            SessionStore(manager.cwd, conv_id).unarchive()

    async def _open_conversation(manager: ConversationManager, request: Request) -> dict:
        body = await _read_json(request)
        conv_id = body.get("resume") if isinstance(body, dict) else None
        if conv_id is not None and not isinstance(conv_id, str):
            raise HTTPException(400, "resume must be a conversation id string")
        if conv_id is not None and not _valid_conv_id(conv_id):
            raise HTTPException(400, "invalid conversation id")
        if conv_id:
            _revive(manager, conv_id)
        conv = manager.open(conv_id)
        return {"conv_id": conv.conv_id}

    async def _models(manager: ConversationManager, refresh: bool) -> list[dict]:
        out = []
        for m in await manager.models(refresh=refresh):
            out.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "context_length": m.context_length,
                    "prompt_price": m.prompt_price,
                    "completion_price": m.completion_price,
                    "supports_tools": m.supports_tools,
                }
            )
        return out

    # ---- plugin kernel helpers ----

    def _registry_for(manager: ConversationManager):
        """A plugin registry describing what this project actually runs.

        Built per request rather than cached: it reads the settings files, and
        a Settings page that showed a stale answer would be worse than a few
        milliseconds of file IO.
        """
        from quickcode.kernel import build_registry

        return build_registry(
            manager.cwd,
            tools=list(manager.registry_factory().tools.values()),
            env=manager.env,
            active_provider=manager.config.profile.provider,
        )

    def _kernel_payload(manager: ConversationManager) -> dict:
        registry = _registry_for(manager)
        payload = registry.to_json()
        payload["mcp_servers"] = list(manager.mcp_servers)
        payload["preset"] = preset_module.resolve(manager.cwd).to_dict()
        return payload

    def _plugin_detail(manager: ConversationManager, plugin_id: str) -> dict:
        registry = _registry_for(manager)
        try:
            return registry.plugin_json(plugin_id, include_view=True)
        except UnknownPlugin as exc:
            raise HTTPException(404, str(exc)) from exc

    async def _update_plugin(
        manager: ConversationManager, plugin_id: str, request: Request
    ) -> dict:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        registry = _registry_for(manager)
        confirmed = bool(body.get("confirmed"))
        try:
            if "enabled" in body:
                registry.set_enabled(plugin_id, bool(body["enabled"]))
            settings = body.get("settings")
            if isinstance(settings, dict):
                for key, value in settings.items():
                    registry.set_setting(plugin_id, key, value, confirmed=confirmed)
            # Inside the try as well: an unknown id reaches here when the body
            # carried nothing to write, and it deserves the same 404 as one
            # that did rather than an unhandled 500.
            return registry.plugin_json(plugin_id, include_view=True)
        except UnknownPlugin as exc:
            raise HTTPException(404, str(exc)) from exc
        except UnknownSetting as exc:
            raise HTTPException(400, str(exc)) from exc
        except LockedSetting as exc:
            raise HTTPException(403, str(exc)) from exc
        except NeedsConfirmation as exc:
            # 409, not 400: the request is valid, it just needs the user to say
            # yes to something the UI must spell out first.
            raise HTTPException(409, exc.reason or str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _presets_payload(manager: ConversationManager) -> dict:
        presets = preset_module.load_presets(manager.cwd)
        active = preset_module.active_preset_id(manager.cwd)
        live = {
            conv_id: conv.store.meta().get("preset", "")
            for conv_id, conv in manager.conversations.items()
        }
        return {
            "active": active,
            "presets": [p.to_dict() for p in presets.values()],
            "live_sessions": live,
        }

    async def _set_active_preset(manager: ConversationManager, request: Request) -> dict:
        body = await _read_json(request)
        preset_id = body.get("preset") if isinstance(body, dict) else None
        if not isinstance(preset_id, str) or not preset_id.strip():
            raise HTTPException(400, "body must be {'preset': <id>}")
        presets = preset_module.load_presets(manager.cwd)
        if preset_id not in presets:
            raise HTTPException(404, f"no preset {preset_id!r}")
        preset_module.set_active(manager.cwd, preset_id)
        # Running sessions keep the preset they began with; this applies to the
        # next one that starts.
        return {"active": preset_id, "applies_to": "new sessions"}

    # ---- permission profiles -------------------------------------------
    #
    # Project-scoped like presets, and for the same reason: a profile lives in
    # a settings file that belongs to a directory, and the trust gate that
    # decides how much of one a project may state is a fact about that
    # directory. The four built-ins ride along in every answer so the picker
    # never has to know they exist.

    def _profiles_payload(manager: ConversationManager) -> dict:
        from quickcode.core import profiles as profiles_module
        from quickcode.security import trust

        cwd = manager.cwd
        found = profiles_module.load_profiles(cwd)
        return {
            "profiles": [p.to_json() for p in found.values()],
            "active": profiles_module.active_profile_id(cwd),
            # What a profile asked for and did not get. The same array the
            # kernel's problems renderer already draws, so a refused profile
            # reads the same wherever it is shown.
            "problems": [p.to_json() for p in profiles_module.profile_problems(cwd)],
            # Whether this project has been trusted, because selecting a profile
            # that widens is gated on it (see ``_set_active_profile``). Sent so
            # the picker can grey the ones that would be refused instead of
            # discovering it on the click -- each profile already carries the
            # ``widens`` half of that answer.
            "trusted": trust.resolve_trust(cwd),
        }

    def _profile_from_body(body: Any):
        """One authored profile out of a request body, or a 400 saying why.

        The parsing is ``PermissionProfile.from_dict``'s, not a second copy of
        it written in route vocabulary: the loader is what decides whether a
        rule is a rule, and an API that disagreed with it would accept things
        that later vanish or refuse things that work when hand-written. The
        difference is only what happens next -- the loader repairs and reports,
        this refuses, because a POST has an author on the other end of it who
        can fix the typo now.
        """
        from quickcode.core.profiles import PermissionProfile

        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        raw_id = body.get("id")
        profile_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise HTTPException(400, (
                f"{raw_id!r} is not a usable profile id: it must start with a "
                "letter or digit and may then contain letters, digits, dots, "
                "dashes and underscores"
            ))
        scope = "project" if body.get("scope") == "project" else "user"
        profile = PermissionProfile.from_dict(
            profile_id, body, layer=scope,  # type: ignore[arg-type]
        )
        if profile.invalid:
            count = len(profile.invalid)
            raise HTTPException(400, (
                f"this profile has {count} "
                f"{'entries' if count != 1 else 'entry'} the permission engine "
                f"can never match ({', '.join(profile.invalid)}). A rule is a "
                "tool name, or a tool name with a pattern in brackets: write, "
                "bash(git *), read(src/**); a mode is one of plan, ask, "
                "auto-edit, dontask, yolo"
            ))
        return profile, scope

    def _save_profile(manager: ConversationManager, body: Any) -> dict:
        from quickcode.core import profiles as profiles_module

        profile, scope = _profile_from_body(body)
        cwd = manager.cwd if scope == "project" else None
        if profile.id in profiles_module.builtin_profiles() and not body.get("shadow"):
            # 409 rather than 400 or a silent write, the same shape
            # ``_update_plugin`` uses for a confirmable refusal: the request is
            # valid, it just does something the user has to have meant. A
            # built-in cannot be edited in place -- saving over its id writes a
            # *shadow* that hides it everywhere, including in the picker, where
            # the title stays the same and the meaning does not. Deleting the
            # shadow brings the built-in back, so this is reversible; it is
            # still not something to do by accident, and the UI's Duplicate
            # button offers a fresh id instead precisely so nobody has to.
            raise HTTPException(409, (
                f"{profile.id!r} is a built-in profile. Saving over it writes a "
                f"copy at {scope} scope that hides the built-in under the same "
                "name. Send 'shadow': true to do that deliberately, or save "
                "under a different id — Duplicate offers one."
            ))
        from quickcode.kernel.state import project_settings_path, user_settings_path

        profiles_module.save_profile(profile, cwd=cwd)
        return {
            "saved": profile.id,
            "scope": scope,
            "path": str(project_settings_path(cwd) if cwd else user_settings_path()),
            # A profile is read at session open and by the switch route; it is
            # not pushed onto running sessions by an edit, the way editing a
            # composition is not.
            "applies_to": "new sessions, and this one if you switch to it",
            **_profiles_payload(manager),
        }

    def _delete_profile(manager: ConversationManager, profile_id: str,
                        scope: str) -> dict:
        from quickcode.core import profiles as profiles_module

        if scope not in ("user", "project"):
            raise HTTPException(400, f"scope must be 'user' or 'project', not {scope!r}")
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise HTTPException(400, f"{profile_id!r} is not a usable profile id")
        cwd = manager.cwd if scope == "project" else None
        if not profiles_module.delete_profile(profile_id, cwd=cwd):
            # Includes trying to delete a built-in that nothing shadows: there
            # is no file to remove it from, and a built-in is shipped rather
            # than owned.
            raise HTTPException(404, (
                f"there is no profile {profile_id!r} at {scope} scope"
            ))
        return {"deleted": profile_id, "scope": scope, **_profiles_payload(manager)}

    def _apply_posture(manager: ConversationManager) -> list[dict]:
        """Push the active posture onto every live session in this project.

        Resolved with no explicit id, so ``active_profile_id`` -- and with it
        the gate that refuses a *selection* an untrusted project makes toward a
        widening profile -- is what decides which one applies. Handing the
        requested id straight to the loader would be that same widening with
        one extra step.

        With no profile selected, each session keeps the mode it is in. Clearing
        a posture is not a request to go back to the install default, and moving
        the mode of a running conversation because a file no longer names a
        profile would be a change nobody asked for.
        """
        from quickcode.core import profiles as profiles_module
        from quickcode.core.permissions import Rules

        cwd = manager.cwd
        base = Rules.load(cwd)
        posture = profiles_module.resolve(cwd)
        out = []
        for conv_id, conv in manager.conversations.items():
            applied = conv.apply_posture(
                posture.mode_enum() if posture else conv.agent.mode,
                posture.merged(base) if posture else base,
                posture,
            )
            out.append({"conv_id": conv_id, **applied})
        return out

    async def _set_active_profile(manager: ConversationManager,
                                  request: Request) -> dict:
        from quickcode.core import profiles as profiles_module
        from quickcode.security import trust

        body = await _read_json(request)
        raw_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(raw_id, str):
            raise HTTPException(400, (
                "body must be {'id': <profile id>}; an empty id clears the "
                "selection and the session runs on the project's own rules"
            ))
        profile_id = raw_id.strip()
        cwd = manager.cwd
        found = profiles_module.load_profiles(cwd)
        if profile_id and profile_id not in found:
            raise HTTPException(400, f"no permission profile {profile_id!r}")
        # The selection is written into the project's settings file, and once it
        # is there nothing can tell it apart from the same line committed by the
        # repository -- which is why ``active_profile_id`` gates it. So a
        # selection that would be gated is refused here, at the point where
        # there is still someone to tell: writing it and then reading it back as
        # nothing is the silent failure the whole module is written against.
        #
        # Refused rather than granted, deliberately. Pointing at a profile that
        # lets the agent act without asking is the same decision trusting the
        # project is, and clicking a picker is not how that decision gets made.
        if profile_id and found[profile_id].widens and not trust.resolve_trust(cwd):
            raise HTTPException(409, (
                f"profile {profile_id!r} lets the agent act without asking, and "
                "this project has not been trusted, so selecting it here would "
                "have no effect. Trust the project to use it, or pick a profile "
                "that only narrows -- those apply to any project."
            ))
        profiles_module.set_active(profile_id, cwd=cwd)
        applied = _apply_posture(manager)
        return {"applied_to": applied, **_profiles_payload(manager)}

    def _prompt_payload(manager: ConversationManager) -> dict:
        text, sections = render_with_sections(
            manager.env,
            model=manager.config.last_model or manager.config.profile.resolve("orchestrator"),
            provider=manager.provider_name,
            orchestration=True,
            overrides=prompt_overrides(manager.cwd),
        )
        return {
            "text": text,
            "sections": [
                {"id": s.id, "title": s.title, "tier": s.tier,
                 "start": s.start, "end": s.end}
                for s in sections
            ],
        }

    # ---- session management: delete, archive, sweep ----

    async def _delete_session(manager: ConversationManager, conv_id: str) -> Response:
        if not _valid_conv_id(conv_id):
            raise HTTPException(404, "unknown conversation")
        # A conversation opened earlier in this run is not "live" -- nothing is
        # attached to it and nothing is running in it. It used to be refused
        # anyway, for the life of the process, with a message telling the user
        # to close something that was not open. Idle ones are closed here;
        # busy ones still say so, and say *why*.
        busy = await manager.release(conv_id)
        if busy:
            raise HTTPException(409, f"conversation is in use ({busy})")
        if not SessionStore(manager.cwd, conv_id).path.exists():
            raise HTTPException(404, "unknown conversation")
        # Everything the session owned goes with it: the transcript, the task
        # board beside it, and any subagent artifact nothing else references.
        try:
            purge_sessions(manager.cwd, [conv_id])
        except OSError as e:
            raise HTTPException(500, f"could not delete session: {e}") from e
        return Response(status_code=204)

    async def _rename_session(
        manager: ConversationManager, conv_id: str, request: Request
    ) -> dict:
        """Give a session a name of its own.

        Deliberately allowed on a *live* conversation, where deleting and
        archiving are refused. Those two move or unlink the log out from under
        its own writer; this appends one ``meta`` record to it, which is what
        every other write to a session log already is — the model change at
        manager.py's ``append_meta(model=…)`` does it mid-conversation too. So
        the answer is 200 and the new name is in effect immediately, for the
        list and for the session that is open.

        A blank title is not an error: it clears a name that was chosen and
        hands the session back to the one derived from its first message. The
        response therefore carries the title the listings will now show, not
        the string that was sent.
        """
        if not _valid_conv_id(conv_id):
            raise HTTPException(404, "unknown conversation")
        store = SessionStore(manager.cwd, conv_id)
        if not store.path.exists():
            raise HTTPException(404, "unknown conversation")
        body = await _read_json(request)
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str):
            raise HTTPException(
                400,
                "body must be {'title': <string>}; an empty title clears the "
                "name and the session goes back to the one taken from its "
                "first message",
            )
        if len(title.strip()) > MAX_TITLE:
            raise HTTPException(400, f"title is longer than {MAX_TITLE} characters")
        try:
            effective = store.rename(title)
        except OSError as e:
            raise HTTPException(500, f"could not rename session: {e}") from e
        return {"conv_id": conv_id, "title": effective}

    async def _set_archived(
        manager: ConversationManager, conv_id: str, archived: bool
    ) -> dict:
        if not _valid_conv_id(conv_id):
            raise HTTPException(404, "unknown conversation")
        store = SessionStore(manager.cwd, conv_id)
        if not store.path.exists():
            raise HTTPException(404, "unknown conversation")
        # Archiving moves the file; doing that under a *running* conversation
        # would pull the log out from beneath its own writer. An idle one is
        # not writing, so it is closed and archived rather than refused for
        # the rest of the process's life.
        if archived:
            busy = await manager.release(conv_id)
            if busy:
                raise HTTPException(409, f"conversation is in use ({busy})")
        try:
            store.archive() if archived else store.unarchive()
        except OSError as e:
            raise HTTPException(500, f"could not move session: {e}") from e
        return {"conv_id": conv_id, "archived": store.archived}

    def _selection(body: Any) -> list[str]:
        ids = body.get("conv_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "body must be {'conv_ids': [<id>, …]}")
        if len(ids) > 500:
            raise HTTPException(400, "too many conversations in one request")
        out = []
        for raw in ids:
            if not isinstance(raw, str) or not _valid_conv_id(raw):
                raise HTTPException(400, f"invalid conversation id: {raw!r}")
            out.append(raw)
        return out

    def _purge_many(manager: ConversationManager, conv_ids: list[str]) -> dict:
        """Delete what can be deleted; report the rest instead of failing whole.

        A bulk delete that aborted on the first live session would leave the
        user guessing which of twenty rows went through.
        """
        skipped: list[dict] = []
        targets: list[str] = []
        for conv_id in conv_ids:
            if manager.get(conv_id) is not None:
                skipped.append({"conv_id": conv_id, "reason": "live"})
            elif not SessionStore(manager.cwd, conv_id).path.exists():
                skipped.append({"conv_id": conv_id, "reason": "missing"})
            else:
                targets.append(conv_id)
        result = purge_sessions(manager.cwd, targets)
        for conv_id in result.missing:
            skipped.append({"conv_id": conv_id, "reason": "missing"})
        return {
            "deleted": result.sessions,
            "boards": result.boards,
            "artifacts": result.artifacts,
            "skipped": skipped,
        }

    async def _bulk_delete(manager: ConversationManager, request: Request) -> dict:
        body = await _read_json(request)
        return _purge_many(manager, _selection(body))

    async def _cleanup_empty(manager: ConversationManager, request: Request) -> dict:
        """Sweep abandoned sessions: no messages *and* no transcript events.

        A launch that was never typed into leaves one of these behind, and
        they bury the real conversations. An interrupted turn does not qualify
        — its event log is the transcript — so it is never swept.
        """
        body = await _read_json(request)
        dry_run = bool(body.get("dry_run")) if isinstance(body, dict) else False
        live = set(manager.conversations)
        candidates = [c for c in SessionStore.empty_sessions(manager.cwd) if c not in live]
        if dry_run:
            return {"candidates": candidates, "deleted": [], "skipped": []}
        return {"candidates": candidates, **_purge_many(manager, candidates)}

    # ---- default-project routes (the original single-project API) ----

    @app.get("/api/bootstrap")
    def bootstrap() -> dict:
        return _bootstrap(hub.default)

    @app.get("/api/sessions")
    def sessions(archived: bool = False) -> list[dict]:
        return _sessions(hub.default, archived)

    # Registered before the ``{conv_id}`` routes so the literal path segments
    # can never be read as a conversation id.
    @app.post("/api/sessions/delete")
    async def bulk_delete_sessions(request: Request) -> dict:
        return await _bulk_delete(hub.default, request)

    @app.post("/api/sessions/cleanup")
    async def cleanup_sessions(request: Request) -> dict:
        return await _cleanup_empty(hub.default, request)

    @app.delete("/api/sessions/{conv_id}")
    async def delete_session(conv_id: str) -> Response:
        return await _delete_session(hub.default, conv_id)

    @app.patch("/api/sessions/{conv_id}")
    async def rename_session(conv_id: str, request: Request) -> dict:
        return await _rename_session(hub.default, conv_id, request)

    @app.post("/api/sessions/{conv_id}/archive")
    async def archive_session(conv_id: str) -> dict:
        return await _set_archived(hub.default, conv_id, True)

    @app.post("/api/sessions/{conv_id}/unarchive")
    async def unarchive_session(conv_id: str) -> dict:
        return await _set_archived(hub.default, conv_id, False)

    @app.post("/api/conversations")
    async def open_conversation(request: Request) -> dict:
        return await _open_conversation(hub.default, request)

    @app.get("/api/models")
    async def models(refresh: bool = False) -> list[dict]:
        return await _models(hub.default, refresh)

    @app.get("/api/plugins")
    def plugins() -> dict:
        return hub.default.plugin_inventory()

    # ---- project registry + browsing ----

    @app.get("/api/projects")
    def projects() -> dict:
        out = []
        for entry in hub.registry.list():
            manager = hub.get(entry.id)
            out.append(
                {
                    "id": entry.id,
                    "path": str(entry.path),
                    "name": entry.name,
                    "last_opened": entry.last_opened,
                    "session_count": _session_count(entry.path),
                    "live_sessions": len(manager.conversations) if manager else 0,
                }
            )
        return {"home": str(Path.home()), "projects": out}

    @app.post("/api/projects/open")
    async def open_project(request: Request) -> dict:
        body = await _read_json(request)
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "body must be {'path': <directory>}")
        try:
            manager = await hub.open(path.strip())
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        # The response shape is a stable contract; the UI learns what was left
        # inert by calling GET /api/projects/{id}/trust right after open.
        return {
            "id": hub.id_of(manager),
            "path": str(manager.cwd),
            "name": manager.cwd.name,
        }

    # ---- forgetting a project: the list entry, or QuickCode's data for it ----
    #
    # Two clearly separate acts, never one route with a mood:
    #
    #   DELETE /api/projects/{pid}        drops the registry entry. Nothing
    #                                     inside the project is touched.
    #   DELETE /api/projects/{pid}/data   additionally deletes
    #                                     <project>/.quickcode and the project's
    #                                     entry in the trust store.
    #
    # Neither ever removes the project directory itself, and neither can reach
    # anything outside <project>/.quickcode — the containment proof lives in
    # session.store.project_data_dir and raises rather than guessing, which
    # surfaces here as a 400.
    #
    # Both refuse a project with live conversations (409), which is the answer
    # DELETE /api/sessions/{conv_id} already gives for a live session.

    async def _forget(pid: str, *, purge_data: bool) -> dict:
        try:
            return await hub.forget(pid, purge_data=purge_data)
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e
        except ProjectBusyError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(500, f"could not delete project data: {e}") from e

    def _data_summary(pid: str) -> dict:
        try:
            return {"id": pid, "project": str(hub.path_of(pid)), **hub.data_summary(pid)}
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    async def _forget_many(request: Request, *, purge_data: bool) -> dict:
        """Remove a selection, reporting each id that could not be removed.

        Same contract as the bulk session delete: what worked is in ``removed``,
        what did not is in ``skipped`` with a reason, and the response is a 200
        either way. A bulk action that failed whole on the first live project
        would leave the user guessing which of ten rows went through.
        """
        body = await _read_json(request)
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "body must be {'ids': [<project id>, …]}")
        if len(ids) > 200:
            raise HTTPException(400, "too many projects in one request")
        removed: list[dict] = []
        skipped: list[dict] = []
        for raw in ids:
            if not isinstance(raw, str):
                raise HTTPException(400, f"invalid project id: {raw!r}")
            try:
                removed.append(await hub.forget(raw, purge_data=purge_data))
            except KeyError:
                skipped.append({"id": raw, "reason": "unknown"})
            except ProjectBusyError as e:
                skipped.append({"id": raw, "reason": "live", "detail": str(e)})
            except (ValueError, OSError) as e:
                skipped.append({"id": raw, "reason": "failed", "detail": str(e)})
        return {"removed": removed, "skipped": skipped}

    # Literal segments first, so neither can be read as a project id.
    @app.post("/api/projects/remove")
    async def remove_projects(request: Request) -> dict:
        return await _forget_many(request, purge_data=False)

    @app.post("/api/projects/purge")
    async def purge_projects(request: Request) -> dict:
        return await _forget_many(request, purge_data=True)

    @app.get("/api/data")
    def data_summary() -> dict:
        return _data_summary(hub.default_id)

    @app.delete("/api/data")
    async def purge_data() -> dict:
        return await _forget(hub.default_id, purge_data=True)

    @app.get("/api/projects/{pid}/data")
    def project_data_summary_route(pid: str) -> dict:
        return _data_summary(pid)

    @app.delete("/api/projects/{pid}/data")
    async def project_purge_data(pid: str) -> dict:
        return await _forget(pid, purge_data=True)

    @app.delete("/api/projects/{pid}")
    async def remove_project(pid: str) -> dict:
        return await _forget(pid, purge_data=False)

    @app.get("/api/dir")
    def browse_dir(path: str | None = None) -> dict:
        try:
            return list_dirs(path)
        except NotADirectoryError as e:
            raise HTTPException(400, f"not a directory: {e}") from e
        except OSError as e:
            raise HTTPException(400, f"cannot read directory: {e}") from e

    # ---- project trust gate ----
    # A project's MCP servers (executable-bearing project-scope config) are
    # inert until the project is explicitly trusted once. These routes let the
    # UI report what was refused and grant/revoke that trust. See
    # docs/TRUST-HANDOFF.md and quickcode/security/trust.py.

    def _with_tool_detail(pid: str, status: dict) -> dict:
        """Add each command tool's argv to the report.

        The trust module hashes those files but cannot parse them -- security
        sits below the kernel, and the authoring parser imports it. The server
        sits above both, so this is the layer where the two can meet.
        """
        if not status.get("tools"):
            return status
        try:
            from quickcode.kernel.authoring import discovery

            detail = discovery.tools_for_review(_project(pid).cwd)
        except Exception:  # a report that loses its detail still reports
            return status
        return {**status, "tool_detail": detail}

    def _trust_status(pid: str) -> dict:
        try:
            return _with_tool_detail(pid, hub.trust_status(pid))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e

    async def _grant_trust(pid: str) -> dict:
        try:
            return _with_tool_detail(pid, await hub.grant_trust(pid))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e

    def _revoke_trust(pid: str) -> dict:
        try:
            return _with_tool_detail(pid, hub.revoke_trust(pid))
        except KeyError as e:
            raise HTTPException(404, f"unknown project: {pid}") from e

    @app.get("/api/trust")
    def trust_status() -> dict:
        return _trust_status(hub.default_id)

    @app.post("/api/trust")
    async def grant_trust() -> dict:
        return await _grant_trust(hub.default_id)

    @app.delete("/api/trust")
    def revoke_trust() -> dict:
        return _revoke_trust(hub.default_id)

    @app.get("/api/projects/{pid}/trust")
    def project_trust_status(pid: str) -> dict:
        return _trust_status(pid)

    @app.post("/api/projects/{pid}/trust")
    async def project_grant_trust(pid: str) -> dict:
        return await _grant_trust(pid)

    @app.delete("/api/projects/{pid}/trust")
    def project_revoke_trust(pid: str) -> dict:
        return _revoke_trust(pid)

    # ---- project-scoped routes ----

    @app.get("/api/projects/{pid}/bootstrap")
    def project_bootstrap(pid: str) -> dict:
        return {**_bootstrap(_project(pid)), "id": pid}

    @app.get("/api/projects/{pid}/sessions")
    def project_sessions(pid: str, archived: bool = False) -> list[dict]:
        return _sessions(_project(pid), archived)

    @app.post("/api/projects/{pid}/sessions/delete")
    async def project_bulk_delete_sessions(pid: str, request: Request) -> dict:
        return await _bulk_delete(_project(pid), request)

    @app.post("/api/projects/{pid}/sessions/cleanup")
    async def project_cleanup_sessions(pid: str, request: Request) -> dict:
        return await _cleanup_empty(_project(pid), request)

    @app.delete("/api/projects/{pid}/sessions/{conv_id}")
    async def project_delete_session(pid: str, conv_id: str) -> Response:
        return await _delete_session(_project(pid), conv_id)

    @app.patch("/api/projects/{pid}/sessions/{conv_id}")
    async def project_rename_session(pid: str, conv_id: str, request: Request) -> dict:
        return await _rename_session(_project(pid), conv_id, request)

    @app.post("/api/projects/{pid}/sessions/{conv_id}/archive")
    async def project_archive_session(pid: str, conv_id: str) -> dict:
        return await _set_archived(_project(pid), conv_id, True)

    @app.post("/api/projects/{pid}/sessions/{conv_id}/unarchive")
    async def project_unarchive_session(pid: str, conv_id: str) -> dict:
        return await _set_archived(_project(pid), conv_id, False)

    @app.post("/api/projects/{pid}/conversations")
    async def project_open_conversation(pid: str, request: Request) -> dict:
        return await _open_conversation(_project(pid), request)

    @app.get("/api/projects/{pid}/models")
    async def project_models(pid: str, refresh: bool = False) -> list[dict]:
        return await _models(_project(pid), refresh)

    @app.get("/api/projects/{pid}/plugins")
    def project_plugins(pid: str) -> dict:
        return _project(pid).plugin_inventory()

    # ---- plugin kernel: what this install consists of, and what may change ----

    @app.get("/api/projects/{pid}/kernel")
    def project_kernel(pid: str) -> dict:
        return _kernel_payload(_project(pid))

    @app.get("/api/kernel")
    def kernel() -> dict:
        return _kernel_payload(hub.default)

    @app.get("/api/projects/{pid}/kernel/plugins/{plugin_id}")
    def project_plugin_detail(pid: str, plugin_id: str) -> dict:
        return _plugin_detail(_project(pid), plugin_id)

    @app.get("/api/kernel/plugins/{plugin_id}")
    def plugin_detail(plugin_id: str) -> dict:
        return _plugin_detail(hub.default, plugin_id)

    @app.put("/api/projects/{pid}/kernel/plugins/{plugin_id}")
    async def project_plugin_update(pid: str, plugin_id: str, request: Request) -> dict:
        return await _update_plugin(_project(pid), plugin_id, request)

    @app.put("/api/kernel/plugins/{plugin_id}")
    async def plugin_update(plugin_id: str, request: Request) -> dict:
        return await _update_plugin(hub.default, plugin_id, request)

    @app.get("/api/projects/{pid}/presets")
    def project_presets(pid: str) -> dict:
        return _presets_payload(_project(pid))

    @app.get("/api/presets")
    def presets() -> dict:
        return _presets_payload(hub.default)

    @app.put("/api/projects/{pid}/presets/active")
    async def project_set_preset(pid: str, request: Request) -> dict:
        return await _set_active_preset(_project(pid), request)

    @app.put("/api/presets/active")
    async def set_preset(request: Request) -> dict:
        return await _set_active_preset(hub.default, request)

    # Registered before the ``{profile_id}`` route so the literal ``active``
    # segment can never be read as a profile id.
    @app.post("/api/profiles/active")
    async def set_active_profile(request: Request) -> dict:
        return await _set_active_profile(hub.default, request)

    @app.post("/api/projects/{pid}/profiles/active")
    async def project_set_active_profile(pid: str, request: Request) -> dict:
        return await _set_active_profile(_project(pid), request)

    @app.get("/api/profiles")
    def profiles() -> dict:
        return _profiles_payload(hub.default)

    @app.get("/api/projects/{pid}/profiles")
    def project_profiles(pid: str) -> dict:
        return _profiles_payload(_project(pid))

    @app.post("/api/profiles")
    async def save_profile(request: Request) -> dict:
        return _save_profile(hub.default, await _read_json(request))

    @app.post("/api/projects/{pid}/profiles")
    async def project_save_profile(pid: str, request: Request) -> dict:
        return _save_profile(_project(pid), await _read_json(request))

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str, scope: str = "user") -> dict:
        return _delete_profile(hub.default, profile_id, scope)

    @app.delete("/api/projects/{pid}/profiles/{profile_id}")
    def project_delete_profile(pid: str, profile_id: str, scope: str = "user") -> dict:
        return _delete_profile(_project(pid), profile_id, scope)

    @app.get("/api/projects/{pid}/prompt")
    def project_prompt(pid: str) -> dict:
        return _prompt_payload(_project(pid))

    @app.get("/api/prompt")
    def prompt() -> dict:
        return _prompt_payload(hub.default)

    @app.get("/api/credits")
    async def credits() -> dict:
        """What is left to spend at the provider, when it will say.

        Added after a run stopped on `402 Insufficient credits`: the balance
        decided whether the next request would work, and there was nowhere to
        see it. Never fails the request -- an unreachable provider answers
        `supported: true, error: "..."`, which the UI shows as "unknown".
        """
        from quickcode.providers import credits as credits_mod

        profile = hub.config.profile
        return await credits_mod.fetch(profile.base_url, profile.api_key)

    @app.put("/api/config")
    async def put_config(request: Request) -> Response:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        # Config is per install, not per project: the default manager's handle
        # is the same object every project shares.
        cfg = hub.config
        theme = body.get("theme")
        if isinstance(theme, dict):
            cfg.theme = {
                k: v for k, v in theme.items() if isinstance(k, str) and isinstance(v, str)
            }
        default_mode = body.get("default_mode")
        if isinstance(default_mode, str):
            cfg.default_mode = default_mode
        base_url = body.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            cfg.profile.base_url = base_url.strip()
        search = body.get("search")
        if isinstance(search, dict):
            _apply_search(cfg, search)
        if "max_tokens" in body:
            # Clamped in `Config`, because config.json is hand-editable and this
            # number is what the provider reserves credit against.
            cfg.max_tokens = _int_or(body.get("max_tokens"), cfg.max_tokens)
        if "temperature" in body:
            cfg.temperature = _float_or_none(body.get("temperature"))
        if "allow_yolo" in body:
            # Arming, not entering. This says yolo may be *reached*; the mode
            # switch and the profile still have to ask for it, and the
            # composition's ceiling still caps it. Without this the only way in
            # was a launch flag nobody can pass to a desktop shortcut, so a
            # profile saying `mode: yolo` was rewritten to `ask` in silence.
            cfg.allow_yolo = bool(body.get("allow_yolo"))
        cfg.save()
        return Response(status_code=204)

    def _apply_search(cfg, block: dict) -> None:
        """Merge a ``search`` block into the config: choice and settings only.

        Every value that reaches here is written to ``~/.quickcode/config.json``
        in plain text, so only non-secret settings are accepted — an ``api_key``
        is refused by name rather than dropped in silence, because a caller that
        thinks it saved a key and did not is the worse failure. Keys go to
        POST /api/search-key and into the encrypted store.

        The per-provider settings merge rather than replace: a key somebody put
        in config.json by hand is theirs, and editing a base URL must not take
        it away.
        """
        from quickcode.search import PROVIDERS, info_for

        provider = ""
        raw_provider = block.get("provider")
        if isinstance(raw_provider, str) and raw_provider.strip():
            provider = raw_provider.strip()
            if provider not in PROVIDERS:
                raise HTTPException(400, f"unknown search provider: {provider!r}")

        updates: dict[str, dict[str, str]] = {}
        per_provider = block.get("providers")
        if isinstance(per_provider, dict):
            for name, values in per_provider.items():
                if name not in PROVIDERS:
                    raise HTTPException(400, f"unknown search provider: {name!r}")
                if not isinstance(values, dict):
                    raise HTTPException(400, f"search.providers.{name} must be an object")
                allowed = {"base_url", *(key for key, _var, _label in info_for(name).extra_fields)}
                for key, value in values.items():
                    if key not in allowed:
                        raise HTTPException(400, _rejected_setting(name, key, allowed))
                    if isinstance(value, str):
                        updates.setdefault(name, {})[key] = value.strip()

        # Nothing above wrote anything: a block that is refused halfway leaves
        # no half-applied settings behind for the next save to pick up.
        if provider:
            cfg.search.provider = provider
        for name, values in updates.items():
            cfg.search.providers.setdefault(name, {}).update(values)

    @app.post("/api/apikey")
    async def put_api_key(request: Request) -> Response:
        from quickcode import secrets

        body = await _read_json(request)
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(400, "body must be {'key': <non-empty string>}")
        secrets.save_api_key(key.strip())
        return Response(status_code=204)

    @app.post("/api/search-key")
    async def put_search_key(request: Request) -> Response:
        """A web-search provider's key, into the same encrypted store.

        Unscoped like /api/apikey and for the same reason: a key belongs to the
        account that issued it, not to the directory that happens to be open.
        Separate from /api/config because config.json is plain text.
        """
        from quickcode import secrets
        from quickcode.search import PROVIDERS, secret_name

        body = await _read_json(request)
        provider = body.get("provider") if isinstance(body, dict) else None
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise HTTPException(400, f"unknown search provider: {provider!r}")
        info = PROVIDERS[provider].info
        if not info.needs_key:
            # Storing one would leave a secret on disk that nothing ever reads.
            raise HTTPException(400, f"{info.label} takes no API key")
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(400, "body must be {'provider': <name>, 'key': <non-empty string>}")
        secrets.save_secret(secret_name(provider), key.strip())
        return Response(status_code=204)

    # ---- update checking ----
    # Install-wide, like /api/config: which version is running is not a
    # property of the directory that happens to be open. Unscoped for the same
    # reason, and never reached from the agent loop — a check is a REST call
    # the UI makes at boot, so it cannot interrupt a running turn.
    #
    # GET is the only route that may talk to github.com, and only when a check
    # is due (see quickcode/update.py for the interval and the off switch). It
    # never raises for a dead network: "could not reach it" comes back as a
    # normal 200 with state "unknown", which is what lets the chrome stay
    # silent about it while the Install page spells it out.

    @app.get("/api/update")
    async def update_status(force: bool = False) -> dict:
        from quickcode import update as update_module

        status = await update_module.check(cwd=hub.default.cwd, force=force)
        return status.to_json()

    @app.put("/api/update/settings")
    async def update_settings(request: Request) -> dict:
        from quickcode import update as update_module

        body = await _read_json(request)
        if not isinstance(body, dict) or not isinstance(
            body.get(update_module.AUTO_CHECK_KEY), bool
        ):
            raise HTTPException(
                400, f"body must be {{'{update_module.AUTO_CHECK_KEY}': true|false}}"
            )
        try:
            update_module.set_auto_check(body[update_module.AUTO_CHECK_KEY])
        except update_module.UpdateError as exc:
            raise HTTPException(500, str(exc)) from exc
        # Answering with the whole status keeps the page from having to guess
        # what switching it off did to everything else on it.
        status = await update_module.check(cwd=hub.default.cwd)
        return status.to_json()

    @app.post("/api/update/download")
    async def update_download() -> dict:
        """Fetch the release installer and verify it. Never runs anything.

        409 is the checksum refusal, and by the time it is raised the bytes
        are already deleted — see quickcode/update.py.
        """
        from quickcode import update as update_module

        status = await update_module.check(cwd=hub.default.cwd)
        try:
            download = await update_module.download_installer(status)
        except update_module.ChecksumMismatch as exc:
            log.error("refused a downloaded installer: %s", exc)
            raise HTTPException(409, str(exc)) from exc
        except update_module.UpdateError as exc:
            raise HTTPException(400, str(exc)) from exc
        return download.to_json()

    @app.post("/api/update/install")
    async def update_install(request: Request) -> dict:
        """Run a verified installer, on an explicit click and a named digest.

        The body must carry the exact SHA-256 the user was shown beside the
        button, so the confirmation is about specific bytes rather than about
        a filename. The file is hashed again before it is executed.
        """
        from quickcode import update as update_module

        body = await _read_json(request)
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        if body.get("confirm") is not True:
            raise HTTPException(400, "body must carry {'confirm': true}")
        path = body.get("path")
        sha256 = body.get("sha256")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(400, "body must name the downloaded 'path'")
        if not isinstance(sha256, str) or not sha256.strip():
            raise HTTPException(400, "body must carry the 'sha256' that was shown")
        try:
            return update_module.launch_installer(Path(path), expected=sha256)
        except update_module.ChecksumMismatch as exc:
            raise HTTPException(409, str(exc)) from exc
        except update_module.UpdateError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not start the installer: {exc}") from exc

    # ---- WebSocket ----

    async def _attach(ws: WebSocket, manager: ConversationManager | None, conv_id: str) -> None:
        if not _ws_allowed(ws):
            await ws.close(code=4403)
            return
        await ws.accept(subprotocol=(auth.SUBPROTOCOL_PREFIX + token) if token else None)
        if manager is None:
            await ws.close(code=4404)
            return
        conv = manager.get(conv_id)
        if conv is None:
            # Attaching to an on-disk session revives it; unknown ids 404.
            if not _valid_conv_id(conv_id):
                await ws.close(code=4404)
                return
            store = SessionStore(manager.cwd, conv_id)
            if not store.path.exists():
                await ws.close(code=4404)
                return
            _revive(manager, conv_id)
            conv = manager.open(conv_id)

        client = Client()
        # Attach BEFORE snapshotting the log: anything logged after this point
        # reaches the live queue, and the client dedupes replays by seq.
        conv.clients.add(client)
        try:
            await ws.send_text(json.dumps(conv.state_event(), ensure_ascii=False))
            await ws.send_text('{"type": "replay_start"}')
            for ev in conv.store.replay_events():
                await ws.send_text(json.dumps(ev, ensure_ascii=False))
            await ws.send_text('{"type": "replay_done"}')
            await _live_phase(ws, conv, client)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            conv.clients.discard(client)

    @app.websocket("/ws/conversation/{conv_id}")
    async def ws_conversation(ws: WebSocket, conv_id: str) -> None:
        await _attach(ws, hub.default, conv_id)

    @app.websocket("/ws/projects/{pid}/conversation/{conv_id}")
    async def ws_project_conversation(ws: WebSocket, pid: str, conv_id: str) -> None:
        await _attach(ws, hub.get(pid), conv_id)

    # Both git shapes resolve their manager lazily, so the hub's default is
    # read per request and a project opened after startup is addressable.
    register_git_routes(app, lambda: hub.default, _project)
    register_path_routes(app, lambda: hub.default, _project)
    # Authored plugins: list, create, read, save, delete, validate, duplicate,
    # and the problems array. Registered from their own module for the same
    # reason the git routes are -- app.py's diff for a whole feature is two
    # lines. Declared after the kernel routes so the literal ``authored``
    # segment cannot be read as a plugin id.
    register_authoring_routes(app, lambda: hub.default, _project)
    # The agent workbench: inventory, resolved composition with provenance,
    # preview from an unsaved draft, and the session-scoped switch. Takes the
    # hub rather than the two lambdas because the session routes reach a
    # conversation by id, not only the default project.
    register_agent_routes(app, hub)
    # The terminal panel's sockets. Handed `_ws_allowed` and the token rather
    # than re-deriving them, so there is exactly one WebSocket auth rule in
    # this app and the shell socket is behind that one, not a second copy.
    register_terminal_routes(app, hub, ws_allowed=_ws_allowed, token=token)

    # mounted last so /api and /ws routes win; skipped when frontend/ absent (tests)
    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


async def _live_phase(ws: WebSocket, conv: Conversation, client: Client) -> None:
    out = asyncio.ensure_future(_pump_out(ws, client))
    inp = asyncio.ensure_future(_pump_in(ws, conv))
    try:
        done, pending = await asyncio.wait({out, inp}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        for task in (out, inp):
            if not task.done():
                task.cancel()
        await asyncio.gather(out, inp, return_exceptions=True)


# A frame every so often, so silence means something. After a laptop sleeps and
# wakes, a socket can sit in OPEN with nothing alive behind it: sends succeed
# into the void and, on an idle conversation, no frame ever arrives to disprove
# it. The client cannot tell that apart from "the agent is thinking" without a
# beat to miss, so this is that beat — and a send that raises here is how this
# side learns the peer is gone, too.
HEARTBEAT_S = 15.0


async def _pump_out(ws: WebSocket, client: Client) -> None:
    while True:
        try:
            item = await asyncio.wait_for(client.queue.get(), timeout=HEARTBEAT_S)
        except TimeoutError:
            await ws.send_text('{"type":"heartbeat"}')
            continue
        if item is None:  # overflow sentinel: force a clean replay reconnect
            await ws.close(code=1013, reason="client fell behind; reconnect to replay")
            return
        await ws.send_text(item)


async def _pump_in(ws: WebSocket, conv: Conversation) -> None:
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue  # malformed frame: drop it rather than kill the socket
        if not isinstance(msg, dict):
            continue
        _dispatch(conv, msg)


# Client message types registered by plugins. Kept separate from the built-in
# handlers below and checked first, but a plugin cannot claim a built-in type:
# the frontend's own protocol must keep meaning what it says.
_CLIENT_HANDLERS: dict[str, Any] = {}

BUILTIN_CLIENT_TYPES = frozenset({
    "user_message", "interrupt", "set_mode", "set_model", "compact",
    "permission_decision", "plan_decision",
})


def register_client_message(kind: str, handler) -> None:
    """Accept a new client → server message type.

    ``handler`` takes ``(conversation, message)``. Registering is additive:
    an unknown type is ignored rather than an error, so an older build simply
    does nothing with a message it has never heard of.
    """
    if kind in BUILTIN_CLIENT_TYPES:
        raise ValueError(f"{kind!r} is a built-in client message type")
    _CLIENT_HANDLERS[kind] = handler


def _dispatch(conv: Conversation, msg: dict[str, Any]) -> None:
    t = msg.get("type")
    handler = _CLIENT_HANDLERS.get(t) if isinstance(t, str) else None
    if handler is not None:
        try:
            handler(conv, msg)
        except Exception:
            log.warning("client message handler for %r failed", t, exc_info=True)
        return
    if t == "user_message":
        text = msg.get("text")
        if isinstance(text, str):
            conv.submit(text)
    elif t == "interrupt":
        conv.interrupt()
    elif t == "set_mode":
        mode = msg.get("mode")
        if isinstance(mode, str):
            conv.set_mode(mode)
    elif t == "set_model":
        model = msg.get("model")
        if isinstance(model, str) and model.strip():
            conv.set_model(model.strip())
    elif t == "compact":
        conv.request_compact()
    elif t == "permission_decision":
        req_id = msg.get("req_id")
        if isinstance(req_id, str):
            conv.resolve_permission(
                req_id,
                allow=bool(msg.get("allow")),
                persist=bool(msg.get("persist")),
                deny_message=str(msg.get("deny_message") or ""),
            )
    elif t == "plan_decision":
        req_id = msg.get("req_id")
        if isinstance(req_id, str):
            mode_after = msg.get("mode_after")
            conv.resolve_plan(
                req_id,
                approved=bool(msg.get("approved")),
                mode_after=mode_after if isinstance(mode_after, str) else None,
                feedback=str(msg.get("feedback") or ""),
            )


async def _read_json(request: Request, maximum: int = JSON_BODY_CAP) -> Any:
    """Read a bounded JSON body without buffering an unbounded request."""
    content_length = request.headers.get("content-length")
    if content_length:
        with contextlib.suppress(ValueError):
            if int(content_length) > maximum:
                raise HTTPException(413, f"request body cannot exceed {maximum} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(413, f"request body cannot exceed {maximum} bytes")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
