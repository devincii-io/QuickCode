"""Google Programmable Search (Custom Search JSON API).

Request:  GET https://www.googleapis.com/customsearch/v1?key=…&cx=…&q=…&num=N
Response: {"items": [{"title", "link", "snippet"}, …]}

Two settings, not one: the API key *and* the search engine id (``cx``) that
says which engine to query. The engine id is not a secret and lives in
config.json or ``QUICKCODE_GOOGLE_CSE_CX``; the key resolves like every other.

This is the one provider that carries its credential in the query string, which
is exactly why :func:`quickcode.search.base.run_search` never puts a URL in an
error message. Free tier is 100 queries a day and the API refuses the 101st.
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

# Google caps num at 10 per request and 400s anything larger.
_MAX_NUM = 10


class GoogleCseProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="google_cse",
        label="Google Programmable Search",
        signup_url="https://programmablesearchengine.google.com/controlpanel/create",
        docs_url="https://developers.google.com/custom-search/v1/overview",
        api_key_env="QUICKCODE_GOOGLE_CSE_API_KEY",
        default_base_url="https://www.googleapis.com/customsearch",
        extra_fields=(
            (
                "cx",
                "QUICKCODE_GOOGLE_CSE_CX",
                "the search engine id (cx) from the Programmable Search control panel",
            ),
        ),
        min_interval_s=0.2,
        free_tier="100 queries/day",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "GET",
            f"{self.base_url}/v1",
            params={
                "key": self.credentials.api_key,
                "cx": self.credentials.extra.get("cx", ""),
                "q": query,
                "num": min(count, _MAX_NUM),
            },
            headers={"Accept": "application/json"},
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        return [
            SearchResult(
                title=first_str(row, "title", "htmlTitle"),
                url=first_str(row, "link", "formattedUrl"),
                snippet=first_str(row, "snippet"),
            )
            for row in rows(payload, "items")
            if first_str(row, "link", "formattedUrl")
        ]
