"""The plugin kernel over HTTP: what this install consists of, and what may change.

The registry snapshot, one plugin's detail and settings, and the presets a
session's agents run. Authored plugins and the agent workbench register from
their own modules (``authoring_api``, ``agents_api``), after these routes.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from quickcode.kernel import preset as preset_module
from quickcode.kernel.spec import (
    LockedSetting,
    NeedsConfirmation,
    UnknownPlugin,
    UnknownSetting,
)
from quickcode.server.http import DEFAULT, PROJECT, read_json, scoped
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub


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


def kernel(manager: ConversationManager) -> dict:
    registry = _registry_for(manager)
    payload = registry.to_json()
    payload["mcp_servers"] = list(manager.mcp_servers)
    payload["preset"] = preset_module.resolve(manager.cwd).to_dict()
    return payload


def plugin_detail(manager: ConversationManager, plugin_id: str) -> dict:
    registry = _registry_for(manager)
    try:
        return registry.plugin_json(plugin_id, include_view=True)
    except UnknownPlugin as exc:
        raise HTTPException(404, str(exc)) from exc


async def plugin_update(
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


def presets(manager: ConversationManager) -> dict:
    found = preset_module.load_presets(manager.cwd)
    active = preset_module.active_preset_id(manager.cwd)
    live = {
        conv_id: conv.preset_id
        for conv_id, conv in manager.conversations.items()
    }
    return {
        "active": active,
        "presets": [p.to_dict() for p in found.values()],
        "live_sessions": live,
    }


async def set_preset(manager: ConversationManager, request: Request) -> dict:
    body = await read_json(request)
    preset_id = body.get("preset") if isinstance(body, dict) else None
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise HTTPException(400, "body must be {'preset': <id>}")
    preset_id = preset_id.strip()
    found = preset_module.load_presets(manager.cwd)
    if preset_id not in found:
        raise HTTPException(404, f"no preset {preset_id!r}")
    preset_module.set_active(manager.cwd, preset_id)
    # Running sessions keep the preset they began with; this applies to the
    # next one that starts.
    return {"active": preset_id, "applies_to": "new sessions"}


def register_kernel_routes(app: FastAPI, hub: ProjectHub) -> None:
    # Project shape first, as these pairs have always been declared.
    for method, suffix, handler in (
        ("GET", "/kernel", kernel),
        ("GET", "/kernel/plugins/{plugin_id}", plugin_detail),
        ("PUT", "/kernel/plugins/{plugin_id}", plugin_update),
        ("GET", "/presets", presets),
        ("PUT", "/presets/active", set_preset),
    ):
        scoped(app, hub, method, suffix, handler, shapes=(PROJECT, DEFAULT))
