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
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from quickcode.kernel import preset as preset_module
from quickcode.kernel.spec import (
    LockedSetting,
    NeedsConfirmation,
    UnknownPlugin,
    UnknownSetting,
)
from quickcode.server import auth, provider_settings
from quickcode.server.agents_api import register_agent_routes
from quickcode.server.authoring_api import register_authoring_routes
from quickcode.server.config_api import register_config_routes, search_payload
from quickcode.server.gitinfo import register_git_routes
from quickcode.server.headers import security_headers
from quickcode.server.http import project, read_json, valid_conv_id, valid_profile_id
from quickcode.server.manager import Client, Conversation, ConversationManager
from quickcode.server.paths import register_path_routes
from quickcode.server.projects import ProjectHub
from quickcode.server.projects_api import register_project_routes
from quickcode.server.prompt_api import prompt_payload
from quickcode.server.terminal import register_terminal_routes
from quickcode.server.update_api import register_update_routes
from quickcode.session.store import (
    MAX_TITLE,
    SessionStore,
    purge_sessions,
)

log = logging.getLogger("quickcode.server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


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
    hardening = security_headers(allowed_hosts)

    def _project(pid: str) -> ConversationManager:
        return project(hub, pid)

    def _token_required(request: Request) -> bool:
        path = request.url.path
        return path.startswith("/api/") and path != "/api/health"

    def _refuse(reason: str) -> Response:
        return Response(f"forbidden: {reason}", status_code=403, headers=hardening)

    @app.middleware("http")
    async def _local_guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return _refuse("bad host")
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return _refuse("bad origin")
        if (token and _token_required(request)
                and not auth.matches(request.headers.get(auth.HEADER), token)):
            return _refuse("bad token")
        response = await call_next(request)
        for name, value in hardening.items():
            response.headers.setdefault(name, value)
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
            prefix = auth.SUBPROTOCOL_PREFIX
            offered = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",")]
            if not any(
                auth.matches(p[len(prefix):], token) for p in offered if p.startswith(prefix)
            ):
                return False
        return True

    # ---- REST ----

    @app.get("/api/health")
    def health(challenge: str = "") -> dict:
        from quickcode.cli import __version__

        out: dict[str, Any] = {"app": "quickcode", "version": __version__}
        # Unauthenticated like the rest of this route, and safe to be: the
        # answer is an HMAC under the token, which does not reveal it. See
        # ``auth.instance_proof`` for who asks and why.
        if token and auth.valid_challenge(challenge):
            out["proof"] = auth.instance_proof(token, port, challenge)
        return out

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
            # The backend plugin (``provider`` above is its display name) and
            # every one Settings can switch to.
            "model_provider": profile.provider,
            "model_providers": provider_settings.payload(cfg),
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "search": search_payload(cfg),
        }

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
        if valid_conv_id(conv_id):
            SessionStore(manager.cwd, conv_id).unarchive()

    async def _open_conversation(manager: ConversationManager, request: Request) -> dict:
        body = await read_json(request)
        conv_id = body.get("resume") if isinstance(body, dict) else None
        if conv_id is not None and not isinstance(conv_id, str):
            raise HTTPException(400, "resume must be a conversation id string")
        if conv_id is not None and not valid_conv_id(conv_id):
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
            active_endpoint=manager.config.profile.base_url,
            model_count=manager.catalog_size(),
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
        body = await read_json(request)
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
            conv_id: conv.preset_id
            for conv_id, conv in manager.conversations.items()
        }
        return {
            "active": active,
            "presets": [p.to_dict() for p in presets.values()],
            "live_sessions": live,
        }

    async def _set_active_preset(manager: ConversationManager, request: Request) -> dict:
        body = await read_json(request)
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
        if not valid_profile_id(profile_id):
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
        if not valid_profile_id(profile_id):
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

        body = await read_json(request)
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

    # ---- session management: delete, archive, sweep ----

    async def _delete_session(manager: ConversationManager, conv_id: str) -> Response:
        if not valid_conv_id(conv_id):
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
            result = purge_sessions(manager.cwd, [conv_id])
        except OSError as e:
            raise HTTPException(500, f"could not delete session: {e}") from e
        if conv_id in result.failed:
            raise HTTPException(500, f"could not delete session: {result.failed[conv_id]}")
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
        if not valid_conv_id(conv_id):
            raise HTTPException(404, "unknown conversation")
        # An open conversation is renamed through its own store. One nobody
        # has spoken in yet has no log on disk -- its opening records are held
        # -- and naming it is an act that writes them, where a second store
        # would find no file and answer 404 for a pane the user can see.
        conv = manager.get(conv_id)
        store = conv.store if conv is not None else SessionStore(manager.cwd, conv_id)
        if conv is None and not store.path.exists():
            raise HTTPException(404, "unknown conversation")
        body = await read_json(request)
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
        if not valid_conv_id(conv_id):
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
            if not isinstance(raw, str) or not valid_conv_id(raw):
                raise HTTPException(400, f"invalid conversation id: {raw!r}")
            out.append(raw)
        return out

    async def _purge_many(manager: ConversationManager, conv_ids: list[str]) -> dict:
        """Delete what can be deleted; report the rest instead of failing whole.

        A bulk delete that aborted on the first live session would leave the
        user guessing which of twenty rows went through.

        "Live" is the same test the single delete applies: an idle conversation
        opened earlier in this run is closed and deleted, not refused for the
        rest of the process's life.
        """
        skipped: list[dict] = []
        targets: list[str] = []
        for conv_id in conv_ids:
            if await manager.release(conv_id):
                skipped.append({"conv_id": conv_id, "reason": "live"})
            elif not SessionStore(manager.cwd, conv_id).path.exists():
                skipped.append({"conv_id": conv_id, "reason": "missing"})
            else:
                targets.append(conv_id)
        result = purge_sessions(manager.cwd, targets)
        for conv_id in result.missing:
            skipped.append({"conv_id": conv_id, "reason": "missing"})
        for conv_id, why in result.failed.items():
            skipped.append({"conv_id": conv_id, "reason": "failed", "detail": why})
        return {
            "deleted": result.sessions,
            "boards": result.boards,
            "artifacts": result.artifacts,
            "skipped": skipped,
        }

    async def _bulk_delete(manager: ConversationManager, request: Request) -> dict:
        body = await read_json(request)
        return await _purge_many(manager, _selection(body))

    async def _cleanup_empty(manager: ConversationManager, request: Request) -> dict:
        """Sweep abandoned sessions: no messages *and* no transcript events.

        A launch that was never typed into leaves one of these behind, and
        they bury the real conversations. An interrupted turn does not qualify
        — its event log is the transcript — so it is never swept.
        """
        body = await read_json(request)
        dry_run = bool(body.get("dry_run")) if isinstance(body, dict) else False
        live = set(manager.live_conversations())
        candidates = [c for c in SessionStore.empty_sessions(manager.cwd) if c not in live]
        if dry_run:
            return {"candidates": candidates, "deleted": [], "skipped": []}
        return {"candidates": candidates, **await _purge_many(manager, candidates)}

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

    register_project_routes(app, hub)

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
        return _save_profile(hub.default, await read_json(request))

    @app.post("/api/projects/{pid}/profiles")
    async def project_save_profile(pid: str, request: Request) -> dict:
        return _save_profile(_project(pid), await read_json(request))

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str, scope: str = "user") -> dict:
        return _delete_profile(hub.default, profile_id, scope)

    @app.delete("/api/projects/{pid}/profiles/{profile_id}")
    def project_delete_profile(pid: str, profile_id: str, scope: str = "user") -> dict:
        return _delete_profile(_project(pid), profile_id, scope)

    # ``?conv=`` answers with the bytes that session is being sent; without it,
    # the prompt the next session starts from. See ``server/prompt_api.py``.
    @app.get("/api/projects/{pid}/prompt")
    def project_prompt(pid: str, conv: str = "") -> dict:
        return prompt_payload(_project(pid), conv)

    @app.get("/api/prompt")
    def prompt(conv: str = "") -> dict:
        return prompt_payload(hub.default, conv)

    register_config_routes(app, hub)
    register_update_routes(app, hub)

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
            if not valid_conv_id(conv_id):
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
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))
        raw = message.get("text")
        if raw is None:
            continue  # a binary frame: the protocol is JSON text, so drop it
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

