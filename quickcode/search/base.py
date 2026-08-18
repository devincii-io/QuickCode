"""Search provider layer: one interface, one result shape, several backends.

The model provider layer (``quickcode/providers``) exists because the runtime
should not know which company answers a chat request. The same argument applies
to search: the ``web_search`` tool asks for results and gets
:class:`SearchResult` objects, and nothing in the tool knows whether Brave,
Serper, Tavily or a self-hosted SearXNG produced them.

A provider is deliberately small. It declares what it is
(:class:`ProviderInfo`), builds one ``httpx.Request`` and parses one JSON body.
Everything that is the same for all of them -- the rate guard, the timeout, the
status-code vocabulary, the redaction rule -- lives in :func:`run_search` here,
so a new provider cannot forget any of it.

**The key never travels anywhere but the request.** No log line, no event, no
``ui_meta`` and no error message includes it. That is why the error path below
reports a host and a status code and never a URL: two of the providers carry
credentials in the query string, and a single "request to <url> failed" would
be a key in a transcript for ever.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

DEFAULT_COUNT = 5
MAX_COUNT = 20
DEFAULT_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class SearchResult:
    """One hit, in the shape the tool renders regardless of who answered.

    ``snippet`` is the short blurb every provider returns. ``content`` is the
    longer extracted page text that only the agent-oriented providers (Tavily,
    Exa) send, and it is empty everywhere else -- the tool prints it when it is
    there rather than asking which provider it came from.
    """

    title: str
    url: str
    snippet: str = ""
    content: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    """What a provider is, in the terms the "you have no key" message needs.

    Every field here exists so an error can name the exact page to visit and
    the exact variable to set for the provider actually in use, rather than
    telling somebody using Serper to go and sign up with Brave.
    """

    name: str
    label: str
    signup_url: str
    docs_url: str = ""
    # Empty for a keyless provider (SearXNG).
    api_key_env: str = ""
    # Providers pointed at an instance rather than a vendor endpoint.
    base_url_env: str = ""
    needs_base_url: bool = False
    default_base_url: str = ""
    # Extra non-secret settings a provider cannot work without, as
    # (config key, env var, human label). Google CSE's engine id is the case.
    extra_fields: tuple[tuple[str, str, str], ...] = ()
    # Client-side spacing between queries, in seconds. Brave's free tier is one
    # query per second and answers a burst with 429s.
    min_interval_s: float = 0.0
    free_tier: str = ""

    @property
    def needs_key(self) -> bool:
        return bool(self.api_key_env)


@dataclass(frozen=True)
class Credentials:
    """Resolved, non-empty-checked inputs for one provider."""

    api_key: str = ""
    base_url: str = ""
    extra: dict[str, str] = field(default_factory=dict)


class SearchError(RuntimeError):
    """A search failed. The message is safe to show a model: no credentials."""


class SearchConfigError(SearchError):
    """The search is not configured -- a key or a base URL is missing.

    Separate from a transport failure because the answer is different: this one
    is fixed by the user pasting something, not by retrying.
    """


@runtime_checkable
class SearchProvider(Protocol):
    """The one interface ``web_search`` depends on."""

    info: ClassVar[ProviderInfo]

    def build_request(self, query: str, count: int) -> httpx.Request:
        """One request, credentials included, ready to send."""
        ...

    def parse(self, payload: Any) -> list[SearchResult]:
        """Normalize this provider's JSON body into the shared result shape."""
        ...


class HttpSearchProvider:
    """Base for the shipped providers: holds credentials, nothing else."""

    info: ClassVar[ProviderInfo]

    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials

    @property
    def base_url(self) -> str:
        return (self.credentials.base_url or self.info.default_base_url).rstrip("/")

    def build_request(self, query: str, count: int) -> httpx.Request:  # pragma: no cover
        raise NotImplementedError

    def parse(self, payload: Any) -> list[SearchResult]:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------
# Rate guard
# --------------------------------------------------------------------------


class RateGuard:
    """Client-side spacing between queries, per provider.

    Brave's free tier allows one query a second, and a model that fires three
    ``web_search`` calls in one round would collect two 429s and burn the
    month's quota learning that. The guard is per provider name because the
    limits differ, and it is client-side only: it delays, it never retries.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, name: str, min_interval_s: float) -> float:
        """Block until another query to ``name`` is due. Returns seconds slept."""
        if min_interval_s <= 0:
            return 0.0
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            now = self._monotonic()
            previous = self._last.get(name)
            delay = 0.0
            if previous is not None:
                delay = max(0.0, min_interval_s - (now - previous))
            if delay > 0:
                await self._sleep(delay)
            self._last[name] = self._monotonic()
            return delay


# One guard for the process: the quota is per key, not per session.
RATE_GUARD = RateGuard()


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

_STATUS_HINTS = {
    400: "the provider rejected the query as malformed",
    401: "the API key was rejected (check it is current and for this provider)",
    403: "the API key was refused (wrong plan, or the key lacks this endpoint)",
    404: "the provider endpoint was not found (check the configured base URL)",
    422: "the provider rejected the query parameters",
    429: "rate limit or monthly quota exhausted -- wait, or check the plan",
}


async def run_search(
    provider: SearchProvider,
    query: str,
    *,
    count: int = DEFAULT_COUNT,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    guard: RateGuard | None = None,
) -> list[SearchResult]:
    """Run one query through one provider and return normalized results.

    Every failure becomes a :class:`SearchError` whose message names the host
    and the status and nothing else. There is no fallback path on purpose: a
    search tool that starts scraping when its key expires is a tool that
    decided by itself to break somebody's terms of service.
    """
    info = provider.info
    count = max(1, min(int(count or DEFAULT_COUNT), MAX_COUNT))
    await (guard or RATE_GUARD).wait(info.name, info.min_interval_s)

    request = provider.build_request(query, count)
    host = request.url.host

    async with httpx.AsyncClient(transport=transport, timeout=timeout_s) as client:
        try:
            response = await client.send(request)
        except httpx.TimeoutException as exc:
            raise SearchError(
                f"{info.label} did not answer within {timeout_s:.0f}s ({host})."
            ) from exc
        except httpx.HTTPError as exc:
            # str(exc) on httpx errors can carry the request URL, and two
            # providers put the key in the query string. Only the class name.
            raise SearchError(
                f"could not reach {info.label} at {host} ({type(exc).__name__})."
            ) from exc

        if response.status_code >= 400:
            hint = _STATUS_HINTS.get(
                response.status_code, "the provider returned an error"
            )
            raise SearchError(
                f"{info.label} returned HTTP {response.status_code} from {host}: {hint}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchError(
                f"{info.label} returned a body that is not JSON ({host})."
            ) from exc

    try:
        results = provider.parse(payload)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SearchError(
            f"could not read {info.label}'s response shape ({type(exc).__name__}). "
            "The provider's API may have changed."
        ) from exc

    return results[:count]


# --------------------------------------------------------------------------
# Parsing helpers, shared by the providers
# --------------------------------------------------------------------------


def first_str(mapping: Any, *keys: str) -> str:
    """The first key present and stringy, else "".

    Providers rename fields between API versions far more often than they
    remove them, so every parser reads a small set of aliases and degrades to
    an empty snippet rather than raising on a rename.
    """
    if not isinstance(mapping, dict):
        return ""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def rows(payload: Any, *keys: str) -> list[dict]:
    """The first list-of-objects found under any of ``keys``."""
    if not isinstance(payload, dict):
        return []
    for key in keys:
        node = payload
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, list):
            return [row for row in node if isinstance(row, dict)]
    return []
