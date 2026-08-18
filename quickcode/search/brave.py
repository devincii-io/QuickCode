"""Brave Search API — the default provider.

Independent index, a free tier of 2,000 queries a month, and a documented
one-query-per-second ceiling on that tier, which is why ``min_interval_s`` is
set here rather than left to the caller.

Request:  GET https://api.search.brave.com/res/v1/web/search?q=…&count=N
          X-Subscription-Token: <key>
Response: {"web": {"results": [{"title", "url", "description"}, …]}}
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


class BraveProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="brave",
        label="Brave Search",
        signup_url="https://api-dashboard.search.brave.com/app/keys",
        docs_url="https://api-dashboard.search.brave.com/app/documentation/web-search",
        api_key_env="QUICKCODE_BRAVE_API_KEY",
        default_base_url="https://api.search.brave.com/res/v1",
        min_interval_s=1.0,
        free_tier="2,000 queries/month, 1 query/second",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "GET",
            f"{self.base_url}/web/search",
            params={"q": query, "count": count, "result_filter": "web"},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self.credentials.api_key,
            },
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        return [
            SearchResult(
                title=first_str(row, "title"),
                url=first_str(row, "url"),
                snippet=first_str(row, "description", "snippet"),
            )
            for row in rows(payload, "web.results", "results")
            if first_str(row, "url")
        ]
