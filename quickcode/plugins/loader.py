"""Plugin discovery: tools and providers via Python entry points.

Third-party packages extend the agent (never the UI) by declaring:

    [project.entry-points."quickcode.tools"]
    mytool = "mypkg.tools:make_tools"       # -> Tool | list[Tool]

    [project.entry-points."quickcode.providers"]
    myprovider = "mypkg.provider:make"      # -> callable(base_url, api_key) -> Provider

A broken plugin is logged and skipped — plugins must never take the app down.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import entry_points

from quickcode.providers.base import Provider
from quickcode.tools.base import Tool

log = logging.getLogger("quickcode.plugins")

TOOLS_GROUP = "quickcode.tools"
PROVIDERS_GROUP = "quickcode.providers"


def load_tool_plugins() -> list[Tool]:
    tools: list[Tool] = []
    for ep in entry_points(group=TOOLS_GROUP):
        try:
            made = ep.load()()
        except Exception as e:
            log.warning("tool plugin %s failed to load: %s", ep.name, e)
            continue
        found = made if isinstance(made, list) else [made]
        for t in found:
            if isinstance(t, Tool) and t.name:
                # Stamped here, not guessed later: provenance is the one thing
                # a plugin cannot be trusted to declare about itself.
                t.source = "entrypoint"
                tools.append(t)
            else:
                log.warning("tool plugin %s returned a non-Tool: %r", ep.name, t)
    return tools


def provider_factories() -> dict[str, Callable[[str, str | None], Provider]]:
    """Built-in providers plus entry-point extras, keyed by config name."""
    from quickcode.providers.openai_compat import OpenAICompatProvider

    factories: dict[str, Callable[[str, str | None], Provider]] = {
        "openai-compat": lambda base_url, api_key: OpenAICompatProvider(base_url, api_key),
    }
    for ep in entry_points(group=PROVIDERS_GROUP):
        try:
            factories[ep.name] = ep.load()
        except Exception as e:
            log.warning("provider plugin %s failed to load: %s", ep.name, e)
    return factories


def make_provider(name: str, base_url: str, api_key: str | None) -> Provider:
    factories = provider_factories()
    factory = factories.get(name)
    if factory is None:
        log.warning("unknown provider %r; falling back to openai-compat", name)
        factory = factories["openai-compat"]
    return factory(base_url, api_key)
