"""Install-wide settings: config.json, the encrypted key store, the credit balance.

None of these routes has a ``/api/projects/{pid}/…`` twin: config and keys belong
to the install and to the account that issued them, not to whichever directory
is open. Settings reads the search half back through the bootstrap
(``search_payload``).
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response

from quickcode.config import _float_or_none, _int_or
from quickcode.server import provider_settings
from quickcode.server.http import read_json
from quickcode.server.projects import ProjectHub


def _rejected_setting(provider: str, key: str, allowed: set[str]) -> str:
    """Why a search setting was refused, in terms of what to do instead."""
    if key == "api_key":
        return (
            "an API key is not saved through this route — POST /api/search-key "
            "puts it in the encrypted store, config.json is plain text"
        )
    return f"{key!r} is not a setting of {provider} (it takes: {', '.join(sorted(allowed))})"


def search_payload(cfg) -> dict:
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


def register_config_routes(app: FastAPI, hub: ProjectHub) -> None:
    @app.get("/api/credits")
    async def credits() -> dict:
        """What is left to spend at the provider, when it will say.

        Added after a run stopped on `402 Insufficient credits`: the balance
        decided whether the next request would work, and there was nowhere to
        see it. Never fails the request -- an unreachable provider answers
        `supported: true, error: "..."`, which the UI shows as "unknown".
        """
        from quickcode.providers import credits as credits_mod
        from quickcode.providers.choice import effective_base_url

        profile = hub.config.profile
        # Where the key is actually sent, not the URL a hand-switched profile
        # still carries: that would post an Anthropic key to OpenRouter.
        return await credits_mod.fetch(effective_base_url(profile), profile.api_key)

    @app.put("/api/config")
    async def put_config(request: Request) -> Response:
        body = await read_json(request)
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
        search = body.get("search")
        if isinstance(search, dict):
            _apply_search(cfg, search)
        # After everything that can refuse the request: a backend switched in
        # memory but never saved or rebuilt would leave the running provider
        # and the config disagreeing, and the next save would not notice.
        backend_changed = provider_settings.apply(cfg, body)
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
        if backend_changed:
            provider_settings.rebuild(hub)
        return Response(status_code=204)

    @app.post("/api/apikey")
    async def put_api_key(request: Request) -> Response:
        from quickcode import secrets

        body = await read_json(request)
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(400, "body must be {'key': <non-empty string>}")
        # Each model provider bills its own account, so the key is stored
        # against the one it belongs to -- the active one unless named.
        target = provider_settings.key_target(hub.config, body)
        secrets.save_provider_key(target, key.strip())
        if target == hub.config.profile.provider:
            provider_settings.rebuild(hub)
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

        body = await read_json(request)
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
