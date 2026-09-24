"""What the native adapter knows about Claude models without asking.

Three questions the request builder has to answer before the first request,
when the Models API may not have been asked yet: which id to send, how thinking
is switched on, and what a response cost. ``capabilities`` from ``GET
/v1/models`` overrides the thinking answer when it has arrived; the static rule
is the fallback for the first request of a session.

Prices are the first-party Messages API rates. A model missing from the table
reports no cost rather than a guessed one -- the ledger treats ``None`` as
unknown, which is the honest answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from quickcode.providers.base import ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_WORKER_MODEL = "claude-sonnet-5"

# Sent when the caller asks for "no cap": the Messages API has no default for
# max_tokens, and every current model accepts at least this much.
FALLBACK_MAX_TOKENS = 32000

# Prompt-cache writes with the default five-minute TTL bill at this multiple
# of the input price; reads at CACHE_READ_MULTIPLIER unless a model says
# otherwise.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float | None = None

    @property
    def cache_read_rate(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * CACHE_READ_MULTIPLIER


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, 0.25),
    "claude-mythos-5-1": Price(10.0, 50.0),
    "claude-fable-5": Price(10.0, 50.0, 1.0),
    "claude-mythos-5": Price(10.0, 50.0, 1.0),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

# Models that predate adaptive thinking: they take ``budget_tokens`` and
# reject ``{"type": "adaptive"}``. Everything else -- including any model
# newer than this table -- is treated as the current surface, because that is
# the direction every release since Opus 4.6 has gone.
_LEGACY = re.compile(
    r"claude-(3[-.]|(opus|sonnet|haiku)-4-(0|1|5)(\b|-)|(opus|sonnet)-4-\d{8})"
)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def normalize_model(model: str) -> str:
    """The Messages API id for a configured model slug.

    Profiles, catalogs and agent definitions are usually written in OpenRouter
    slugs (``anthropic/claude-opus-4.8``); the same model on the native API is
    ``claude-opus-4-8``. Translating here keeps every one of those configs
    working after a provider switch. A slug for another vendor cannot be
    served by this API at all, which is worth saying before a request is made.
    """
    m = (model or "").strip()
    if not m:
        raise ProviderError("no model selected")
    if "/" in m:
        vendor, _, rest = m.partition("/")
        if vendor != "anthropic":
            raise ProviderError(
                f"model {model!r} is not an Anthropic model; the native Anthropic provider "
                "only serves claude-* models"
            )
        m = rest.split(":", 1)[0].replace(".", "-")
    return m


def price_for(model: str) -> Price | None:
    """Longest known id that ``model`` is, or is a dated snapshot of."""
    for known in sorted(PRICES, key=len, reverse=True):
        if model == known or re.fullmatch(re.escape(known) + r"-\d{8}", model):
            return PRICES[known]
    return None


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> float | None:
    """What one response cost. ``input_tokens`` is the uncached remainder."""
    price = price_for(model)
    if price is None:
        return None
    total = (
        input_tokens * price.input
        + cache_write_tokens * price.input * CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * price.cache_read_rate
        + output_tokens * price.output
    )
    return round(total / 1_000_000, 8)


@dataclass(frozen=True)
class Traits:
    """How a model wants thinking and sampling configured."""

    adaptive: bool
    budget: bool
    effort: frozenset[str] = field(default_factory=frozenset)
    max_output: int | None = None


def traits_for(model: str, capabilities: dict[str, Any] | None = None) -> Traits:
    if capabilities:
        from_api = _traits_from_capabilities(capabilities)
        if from_api is not None:
            return from_api
    if _LEGACY.match(model):
        return Traits(adaptive=False, budget=True)
    return Traits(adaptive=True, budget=False, effort=frozenset(EFFORT_LEVELS))


def _supported(node: Any) -> bool:
    return isinstance(node, dict) and node.get("supported") is True


def _traits_from_capabilities(entry: dict[str, Any]) -> Traits | None:
    caps = entry.get("capabilities")
    if not isinstance(caps, dict):
        return None
    thinking = caps.get("thinking") if isinstance(caps.get("thinking"), dict) else {}
    types = thinking.get("types") if isinstance(thinking.get("types"), dict) else {}
    effort = caps.get("effort") if isinstance(caps.get("effort"), dict) else {}
    levels = frozenset(
        lvl for lvl in EFFORT_LEVELS if _supported(effort) and _supported(effort.get(lvl))
    )
    max_output = entry.get("max_tokens")
    return Traits(
        adaptive=_supported(types.get("adaptive")),
        budget=_supported(types.get("enabled")) and not _supported(types.get("adaptive")),
        effort=levels,
        max_output=max_output if isinstance(max_output, int) and max_output > 0 else None,
    )
