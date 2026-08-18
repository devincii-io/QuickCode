"""Tavily — search built for agents: every hit comes back with extracted text.

Request:  POST https://api.tavily.com/search
          Authorization: Bearer <key>
          body {"query": …, "max_results": N, "search_depth": "basic"}
Response: {"results": [{"title", "url", "content", "raw_content", "score"}, …]}

The key goes in the Authorization header rather than in the JSON body, which
the older client did: a body is the one part of a request that ends up in a
debug dump, and a key in a transcript outlives the session it leaked in.
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


class TavilyProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="tavily",
        label="Tavily",
        signup_url="https://app.tavily.com/home",
        docs_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
        api_key_env="QUICKCODE_TAVILY_API_KEY",
        default_base_url="https://api.tavily.com",
        min_interval_s=0.5,
        free_tier="1,000 credits/month",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "POST",
            f"{self.base_url}/search",
            json={
                "query": query,
                "max_results": count,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.credentials.api_key}",
            },
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        return [
            SearchResult(
                title=first_str(row, "title"),
                url=first_str(row, "url"),
                snippet=first_str(row, "content", "snippet", "description"),
                content=first_str(row, "raw_content"),
            )
            for row in rows(payload, "results")
            if first_str(row, "url")
        ]
