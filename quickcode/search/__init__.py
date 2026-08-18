"""Pluggable web search.

``from quickcode.search import resolve_provider, run_search`` is the whole
surface the ``web_search`` tool uses. Adding a backend means writing one module
next to ``brave.py`` and adding a line to ``PROVIDERS`` in ``resolve.py``;
nothing in the tool changes.
"""

from quickcode.search.base import (
    DEFAULT_COUNT,
    MAX_COUNT,
    Credentials,
    ProviderInfo,
    RateGuard,
    SearchConfigError,
    SearchError,
    SearchProvider,
    SearchResult,
    run_search,
)
from quickcode.search.resolve import (
    DEFAULT_PROVIDER,
    KEY_SOURCE_CONFIG,
    KEY_SOURCE_SAVED,
    PROVIDER_CHOICE_ENV,
    PROVIDERS,
    SearchSettings,
    chosen_provider,
    configured_providers,
    info_for,
    key_source,
    provider_infos,
    provider_names,
    resolve_credentials,
    resolve_provider,
    secret_name,
)

__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_PROVIDER",
    "KEY_SOURCE_CONFIG",
    "KEY_SOURCE_SAVED",
    "MAX_COUNT",
    "PROVIDERS",
    "PROVIDER_CHOICE_ENV",
    "Credentials",
    "ProviderInfo",
    "RateGuard",
    "SearchConfigError",
    "SearchError",
    "SearchProvider",
    "SearchResult",
    "SearchSettings",
    "chosen_provider",
    "configured_providers",
    "info_for",
    "key_source",
    "provider_infos",
    "provider_names",
    "resolve_credentials",
    "resolve_provider",
    "run_search",
    "secret_name",
]
