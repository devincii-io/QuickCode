"""Serper — Google results through an API.

Request:  POST https://google.serper.dev/search
          X-API-KEY: <key>   body {"q": …, "num": N}
Response: {"organic": [{"title", "link", "snippet", "position"}, …],
           "answerBox": {…}, "knowledgeGraph": {…}}

Only ``organic`` is normalized. The answer box and knowledge graph are Google
surfaces with their own shapes and no stable URL, and a ranked list whose first
entry is sometimes a different kind of thing is harder for a model to use than
one that is always ten links.
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


class SerperProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="serper",
        label="Serper (Google)",
        signup_url="https://serper.dev/api-key",
        docs_url="https://serper.dev/playground",
        api_key_env="QUICKCODE_SERPER_API_KEY",
        default_base_url="https://google.serper.dev",
        min_interval_s=0.2,
        free_tier="2,500 credits on signup, then pay as you go",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "POST",
            f"{self.base_url}/search",
            json={"q": query, "num": count},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-KEY": self.credentials.api_key,
            },
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        return [
            SearchResult(
                title=first_str(row, "title"),
                url=first_str(row, "link", "url"),
                snippet=first_str(row, "snippet", "description"),
            )
            for row in rows(payload, "organic", "results")
            if first_str(row, "link", "url")
        ]
