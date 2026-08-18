"""Which search provider answers, and where its credentials come from.

Two resolutions live here and they follow the same order, deliberately:

    explicit config  ->  environment variable  ->  (key only) encrypted store

For the *provider choice* that is ``search.provider`` in
``~/.quickcode/config.json``, then ``QUICKCODE_SEARCH_PROVIDER``, then Brave.
For a *credential* it is ``search.providers.<name>.api_key``, then the
provider's own env var, then ``~/.quickcode/search-<name>.key`` -- the same
DPAPI-backed store the OpenRouter key uses, not a second one.

Nothing here ever switches provider on its own. If the chosen provider has no
key and a different one does, the error says so and stops: silently answering a
question with a search engine the user did not pick is a worse failure than not
answering it, because nobody finds out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from quickcode.search.base import (
    DEFAULT_COUNT,
    Credentials,
    ProviderInfo,
    SearchConfigError,
    SearchProvider,
)
from quickcode.search.brave import BraveProvider
from quickcode.search.exa import ExaProvider
from quickcode.search.google_cse import GoogleCseProvider
from quickcode.search.searxng import SearxngProvider
from quickcode.search.serper import SerperProvider
from quickcode.search.tavily import TavilyProvider

DEFAULT_PROVIDER = "brave"
PROVIDER_CHOICE_ENV = "QUICKCODE_SEARCH_PROVIDER"
# Prefix under ~/.quickcode/, so a search key can never collide with the
# OpenRouter one and both are visible as files for whoever wants to revoke them.
SECRET_PREFIX = "search-"

PROVIDERS: dict[str, type] = {
    BraveProvider.info.name: BraveProvider,
    SerperProvider.info.name: SerperProvider,
    TavilyProvider.info.name: TavilyProvider,
    SearxngProvider.info.name: SearxngProvider,
    ExaProvider.info.name: ExaProvider,
    GoogleCseProvider.info.name: GoogleCseProvider,
}


def provider_names() -> list[str]:
    return list(PROVIDERS)


def provider_infos() -> list[ProviderInfo]:
    return [cls.info for cls in PROVIDERS.values()]


def info_for(name: str) -> ProviderInfo:
    try:
        return PROVIDERS[name].info
    except KeyError:
        raise SearchConfigError(_unknown_message(name)) from None


def secret_name(provider: str) -> str:
    """The name this provider's key is stored under in the encrypted store."""
    return f"{SECRET_PREFIX}{provider}"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class SearchSettings:
    """The ``search`` block of ``~/.quickcode/config.json``.

    ``providers`` holds per-provider settings: ``base_url`` for an instance you
    host, ``cx`` for Google's engine id, and -- for people who would rather
    manage one file than one encrypted blob -- ``api_key``. QuickCode reads a
    key from here (and preserves one already written) but never puts one here
    itself: ``set-key`` goes to the encrypted store, because config.json is
    plain text and gets copied between machines.
    """

    provider: str = ""
    max_results: int = DEFAULT_COUNT
    providers: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> SearchSettings:
        if not isinstance(raw, dict):
            return cls()
        per_provider: dict[str, dict[str, str]] = {}
        for name, values in (raw.get("providers") or {}).items():
            if isinstance(values, dict):
                per_provider[str(name)] = {
                    str(k): str(v) for k, v in values.items() if isinstance(v, str | int)
                }
        try:
            max_results = int(raw.get("max_results", DEFAULT_COUNT))
        except (TypeError, ValueError):
            max_results = DEFAULT_COUNT
        return cls(
            provider=str(raw.get("provider", "") or ""),
            max_results=max_results,
            providers=per_provider,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "max_results": self.max_results,
            "providers": {k: dict(v) for k, v in self.providers.items()},
        }

    def for_provider(self, name: str) -> dict[str, str]:
        return self.providers.get(name, {})


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def _load_secret(name: str) -> str | None:
    from quickcode.secrets import load_secret

    return load_secret(name)


def chosen_provider(
    settings: SearchSettings | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """The provider name in force: config, then env, then Brave."""
    env = os.environ if env is None else env
    if settings and settings.provider.strip():
        return settings.provider.strip()
    from_env = (env.get(PROVIDER_CHOICE_ENV) or "").strip()
    return from_env or DEFAULT_PROVIDER


def resolve_credentials(
    info: ProviderInfo,
    settings: SearchSettings | None = None,
    env: dict[str, str] | None = None,
    load=_load_secret,
) -> tuple[Credentials, list[str]]:
    """Credentials for one provider, plus a list of what is still missing.

    The missing list is prose, not keys: it is what the error message reads out
    to somebody who has to go and fix it.
    """
    env = os.environ if env is None else env
    configured = settings.for_provider(info.name) if settings else {}
    missing: list[str] = []

    api_key = ""
    if info.needs_key:
        api_key = (
            configured.get("api_key")
            or env.get(info.api_key_env)
            or (load(secret_name(info.name)) if load else None)
            or ""
        ).strip()
        if not api_key:
            missing.append("an API key")

    base_url = (
        configured.get("base_url")
        or (env.get(info.base_url_env) if info.base_url_env else "")
        or info.default_base_url
        or ""
    ).strip()
    if info.needs_base_url and not base_url:
        missing.append("the base URL of the instance to query")

    extra: dict[str, str] = {}
    for key, var, label in info.extra_fields:
        value = (configured.get(key) or env.get(var) or "").strip()
        if not value:
            missing.append(label)
        extra[key] = value

    return Credentials(api_key=api_key, base_url=base_url, extra=extra), missing


def configured_providers(
    settings: SearchSettings | None = None,
    env: dict[str, str] | None = None,
    load=_load_secret,
) -> list[str]:
    """Every provider that would work right now, in registration order."""
    ready = []
    for name, cls in PROVIDERS.items():
        _, missing = resolve_credentials(cls.info, settings, env, load)
        if not missing:
            ready.append(name)
    return ready


def resolve_provider(
    name: str | None = None,
    *,
    settings: SearchSettings | None = None,
    env: dict[str, str] | None = None,
    load=_load_secret,
) -> SearchProvider:
    """The provider to search with, ready to use.

    Raises :class:`SearchConfigError` -- never a partially configured provider
    and never a substitute one.
    """
    chosen = (name or chosen_provider(settings, env)).strip()
    if chosen not in PROVIDERS:
        raise SearchConfigError(_unknown_message(chosen))

    cls = PROVIDERS[chosen]
    credentials, missing = resolve_credentials(cls.info, settings, env, load)
    if missing:
        raise SearchConfigError(
            _unconfigured_message(cls.info, missing, settings, env, load)
        )
    return cls(credentials)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def _config_path() -> str:
    from quickcode.config import CONFIG_PATH

    return str(CONFIG_PATH)


def _unknown_message(name: str) -> str:
    known = ", ".join(provider_names())
    shown = name or "(empty)"
    return (
        f"unknown search provider {shown!r}. Known providers: {known}. "
        f'Set "search": {{"provider": "brave"}} in {_config_path()} or the '
        f"{PROVIDER_CHOICE_ENV} environment variable."
    )


def _unconfigured_message(
    info: ProviderInfo,
    missing: list[str],
    settings: SearchSettings | None,
    env: dict[str, str] | None,
    load,
) -> str:
    lines = [
        f"web_search is not configured for {info.label}: missing "
        f"{', and '.join(missing)}."
    ]
    tier = f" (free tier: {info.free_tier})" if info.free_tier else ""
    lines.append(f"Get it at {info.signup_url}{tier}.")

    if info.needs_key:
        lines.append(
            f"Then set {info.api_key_env}, or store it encrypted with:  "
            f"python -m quickcode.search set-key {info.name}"
        )
    if info.needs_base_url:
        lines.append(
            f"Then set {info.base_url_env}, or "
            f'"search": {{"providers": {{"{info.name}": {{"base_url": "..."}}}}}} '
            f"in {_config_path()}."
        )
    for key, var, label in info.extra_fields:
        lines.append(f"Set {var} (or search.providers.{info.name}.{key}) to {label}.")

    others = [n for n in configured_providers(settings, env, load) if n != info.name]
    if others:
        labels = ", ".join(PROVIDERS[n].info.label for n in others)
        lines.append(
            f"Configured and ready instead: {labels}. QuickCode will not switch "
            f'on its own -- set "search": {{"provider": "{others[0]}"}} in '
            f"{_config_path()} (or {PROVIDER_CHOICE_ENV}={others[0]}) if that is "
            "what you want."
        )
    return " ".join(lines)
