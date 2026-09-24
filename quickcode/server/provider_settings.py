"""Settings -> model provider: what Settings shows, what a save changes, and
putting the new backend to work without a restart."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

if TYPE_CHECKING:
    from quickcode.config import Config
    from quickcode.server.projects import ProjectHub


def payload(cfg: Config) -> list[dict[str, Any]]:
    """Every selectable backend, with the defaults a switch would apply and
    whether a key for it is available -- never the key itself."""
    from quickcode import secrets
    from quickcode.plugins import loader
    from quickcode.providers.choice import defaults_for

    active = cfg.profile.provider
    out = []
    for name in loader.provider_factories():
        defaults = defaults_for(name)
        out.append({
            "name": name,
            "label": defaults.label if defaults else name,
            "base_url": defaults.base_url if defaults else "",
            "orchestrator_model": defaults.orchestrator_model if defaults else "",
            "active": name == active,
            "api_key_env": secrets.provider_key_env(name),
            "has_api_key": bool(secrets.load_provider_key(name)),
        })
    return out


def apply(cfg: Config, body: dict[str, Any]) -> bool:
    """Merge ``provider`` / ``base_url`` from a config save. Returns whether
    the backend changed, so the caller knows to rebuild it."""
    from quickcode.plugins import loader
    from quickcode.providers.choice import switch_provider

    profile = cfg.profile
    before = (profile.provider, profile.base_url)
    provider = body.get("provider")
    if isinstance(provider, str) and provider.strip():
        name = provider.strip()
        if name not in loader.provider_factories():
            raise HTTPException(400, f"unknown model provider: {name!r}")
        if name != profile.provider:
            switch_provider(cfg, name)
    base_url = body.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        profile.base_url = base_url.strip()
    return (profile.provider, profile.base_url) != before


def key_target(cfg: Config, body: Any) -> str:
    """Which provider a key being saved belongs to: the one named, else the
    active one."""
    from quickcode.plugins import loader

    named = body.get("provider") if isinstance(body, dict) else None
    if named is None:
        return cfg.profile.provider
    if not isinstance(named, str) or named not in loader.provider_factories():
        raise HTTPException(400, f"unknown model provider: {named!r}")
    return named


def rebuild(hub: ProjectHub) -> None:
    from quickcode.plugins import loader

    profile = hub.config.profile
    hub.replace_provider(loader.make_provider(profile.provider, profile.base_url, profile.api_key))
