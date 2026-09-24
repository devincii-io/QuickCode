"""Which built-in model provider a profile uses, and what switching one means.

A profile holds one base URL and two model slugs. Each built-in provider has
its own defaults for all three, and a switch has to tell a value the user chose
(kept) from one that was only the old provider's default (replaced) -- or the
Anthropic key ends up posted to OpenRouter's URL, and the first request asks
the native API for an OpenRouter slug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from quickcode.config import DEFAULT_BASE_URL, Profile

if TYPE_CHECKING:
    from quickcode.config import Config


@dataclass(frozen=True)
class ProviderDefaults:
    name: str
    label: str
    base_url: str
    orchestrator_model: str
    worker_model: str


_OPENAI_COMPAT = Profile()

BUILTIN: dict[str, ProviderDefaults] = {
    "openai-compat": ProviderDefaults(
        name="openai-compat",
        label="OpenRouter / OpenAI-compatible",
        base_url=DEFAULT_BASE_URL,
        orchestrator_model=_OPENAI_COMPAT.orchestrator_model,
        worker_model=_OPENAI_COMPAT.worker_model,
    ),
    "anthropic": ProviderDefaults(
        name="anthropic",
        label="Anthropic (native Messages API)",
        base_url="https://api.anthropic.com",
        orchestrator_model="claude-opus-5",
        worker_model="claude-sonnet-5",
    ),
}


def defaults_for(name: str) -> ProviderDefaults | None:
    return BUILTIN.get(name)


def switch_provider(config: Config, name: str) -> None:
    """Point the active profile at ``name``, keeping what the user set."""
    profile = config.profile
    old, new = BUILTIN.get(profile.provider), BUILTIN.get(name)
    profile.provider = name
    if new is None or old is new:
        return
    if not profile.base_url or old is None or profile.base_url == old.base_url:
        profile.base_url = new.base_url
    if old is None or profile.orchestrator_model == old.orchestrator_model:
        profile.orchestrator_model = new.orchestrator_model
    if old is None or profile.worker_model == old.worker_model:
        profile.worker_model = new.worker_model
    if config.last_model and not model_usable(name, config.last_model):
        config.last_model = ""


def model_usable(provider: str, model: str) -> bool:
    """Whether a remembered model id could be sent to ``provider`` at all."""
    if provider == "anthropic":
        from quickcode.providers.anthropic.models import normalize_model
        from quickcode.providers.base import ProviderError

        try:
            normalize_model(model)
        except ProviderError:
            return False
        return True
    if provider == "openai-compat":
        # A bare ``claude-*`` id is the native API's naming; OpenRouter and
        # every gateway in front of it want the vendor-prefixed slug.
        return not model.startswith("claude-")
    return True


def display_name(profile: Profile) -> str:
    """How the UI and the identity prompt name the backend."""
    if profile.provider == "anthropic":
        from quickcode.providers.anthropic import resolve_base_url

        url = resolve_base_url(profile.base_url)
        return "Anthropic" if url == BUILTIN["anthropic"].base_url else url
    return "OpenRouter" if "openrouter.ai" in profile.base_url else profile.base_url
