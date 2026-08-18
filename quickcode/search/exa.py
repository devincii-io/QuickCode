"""Exa — neural search that returns page text with each hit.

Request:  POST https://api.exa.ai/search
          x-api-key: <key>
          body {"query": …, "numResults": N, "type": "auto",
                "contents": {"text": {"maxCharacters": 1200}}}
Response: {"results": [{"title", "url", "text", "publishedDate", …}, …]}

Exa has no separate snippet field: the extracted ``text`` is both the blurb and
the content, so the head of it becomes the snippet and the whole of it the
content. If the ``contents`` request is ever refused the parser still yields
titles and URLs, which is the degradation this shape is chosen for.
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

_SNIPPET_CHARS = 300


class ExaProvider(HttpSearchProvider):
    info: ClassVar[ProviderInfo] = ProviderInfo(
        name="exa",
        label="Exa",
        signup_url="https://dashboard.exa.ai/api-keys",
        docs_url="https://docs.exa.ai/reference/search",
        api_key_env="QUICKCODE_EXA_API_KEY",
        default_base_url="https://api.exa.ai",
        min_interval_s=0.2,
        free_tier="$10 of credit on signup",
    )

    def build_request(self, query: str, count: int) -> httpx.Request:
        return httpx.Request(
            "POST",
            f"{self.base_url}/search",
            json={
                "query": query,
                "numResults": count,
                "type": "auto",
                "contents": {"text": {"maxCharacters": 1200}},
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": self.credentials.api_key,
            },
        )

    def parse(self, payload: Any) -> list[SearchResult]:
        out: list[SearchResult] = []
        for row in rows(payload, "results"):
            url = first_str(row, "url")
            if not url:
                continue
            text = first_str(row, "text", "summary")
            snippet = text[:_SNIPPET_CHARS].strip()
            if len(text) > _SNIPPET_CHARS:
                snippet += "…"
            out.append(
                SearchResult(
                    title=first_str(row, "title") or url,
                    url=url,
                    snippet=snippet,
                    content=text,
                )
            )
        return out
