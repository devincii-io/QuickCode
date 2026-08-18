"""SearXNG — the keyless option: point it at an instance you trust.

Request:  GET <base-url>/search?q=…&format=json&categories=general
Response: {"results": [{"title", "url", "content", "engine"}, …]}

There is no signup and no key: the configuration is a base URL, which is why
this is the provider that costs nothing to run. Two caveats worth knowing
before pointing it at a public instance: most of them disable the JSON output
format (``formats: [json]`` has to be enabled in the instance's settings.yml,
and a disabled one answers 403), and a public instance sees every query.

Unlike a ``web_fetch`` target, this base URL is *not* held to the loopback and
private-range rules. A self-hosted instance at ``http://localhost:8080`` is the
normal case for this provider, and refusing it would refuse the only reason to
choose it. The difference that makes it safe is who chooses: a fetch URL comes
from the model, this one comes from the user's own config file.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from quickcode.search.base import (
    HttpSearchProvider,
    ProviderInfo,
    SearchResult,
    first_str,
    rows,
)


class SearxngProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="searxng",
        label="SearXNG",
        signup_url="https://docs.searxng.org/admin/installation.html",
        docs_url="https://docs.searxng.org/dev/search_api.html",
        base_url_env="QUICKCODE_SEARXNG_URL",
        needs_base_url=True,
        min_interval_s=0.5,
        free_tier="free; you host it, or you use somebody else's instance",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "GET",
            f"{self.base_url}/search",
            params={
                "q": query,
                "format": "json",
                "categories": "general",
                "language": "en",
            },
            headers={"Accept": "application/json"},
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        return [
            SearchResult(
                title=first_str(row, "title"),
                url=first_str(row, "url"),
                snippet=first_str(row, "content", "snippet"),
            )
            for row in rows(payload, "results")
            if first_str(row, "url")
        ]
