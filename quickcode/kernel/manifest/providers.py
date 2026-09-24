"""One plugin per model provider factory."""

from __future__ import annotations

from typing import Any

from quickcode.kernel.facts import display_endpoint
from quickcode.kernel.spec import PluginSpec

_PROVIDER_DESCRIPTIONS = {
    "openai-compat": "Any OpenAI-compatible endpoint, including OpenRouter.",
    "anthropic": "Anthropic's native Messages API: signed thinking, exact cache "
                 "breakpoints, its own API key.",
}


def provider_specs(factories: dict[str, Any], *, active: str = "", endpoint: str = "",
                   model_count: int | None = None) -> list[PluginSpec]:
    """One plugin per provider factory.

    ``endpoint`` and ``model_count`` describe the *active* one only -- the base
    URL it talks to and how many models its loaded catalog lists (None until a
    catalog has been fetched). An inactive provider has neither yet.
    """
    out: list[PluginSpec] = []
    for name in sorted(factories):
        is_active = name == active
        out.append(PluginSpec(
            id=f"provider.{name}",
            kind="provider",
            title=name,
            description=_PROVIDER_DESCRIPTIONS.get(name, "Model backend."),
            group="Models",
            source="internal" if name in _PROVIDER_DESCRIPTIONS else "entrypoint",
            summary="Supplies the models every agent runs on, for the whole install.",
            affects=("models",),
            audience="install",
            consequence=(
                "This is the backend every session currently talks to: it decides "
                "which model slugs exist, what they cost and where the API key goes."
                if is_active else
                "Not the active backend. Switching to it changes which model slugs "
                "exist and which key is used, for every project on this machine."
            ),
            docs_anchor="docs/ARCHITECTURE.md#provider-layer",
            metadata={"provider": name, "active": is_active,
                      "endpoint": display_endpoint(endpoint) if is_active else "",
                      "model_count": model_count if is_active else None},
        ))
    return out
