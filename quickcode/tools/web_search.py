"""web_search tool: a ranked list of links, from whichever provider is configured.

The tool holds no opinion about search engines. It asks
``quickcode.search.resolve_provider`` who is configured, hands the query over,
and renders the normalized :class:`~quickcode.search.SearchResult` list. Brave,
Serper, Tavily, SearXNG, Exa and Google Programmable Search all arrive here as
the same three fields, so adding a seventh changes nothing in this file.

Two decisions worth reading:

*The tool registers even with no key.* That matches how the OpenRouter key is
handled -- the app launches, the failure surfaces per request -- and it is the
better half of the trade: a tool that vanishes when a key is missing produces a
model that says "I have no way to search", while a tool that fails loudly
produces an error naming the signup page. It also keeps the tool list stable,
which the prompt cache and the plugin registry both care about.

*The provider is never chosen here.* There is no ``provider`` argument on the
schema, so the model cannot shop around, and there is no fallback when a key
expires. In particular there is no scraping path: a search tool that starts
scraping when its API key stops working is a tool that decided by itself to
break somebody's terms of service.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from quickcode.context import toon
from quickcode.search import (
    DEFAULT_COUNT,
    MAX_COUNT,
    SearchConfigError,
    SearchError,
    SearchResult,
    resolve_provider,
    run_search,
)
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult

SNIPPET_CHARS = 400
CONTENT_CHARS = 1200


class WebSearchInput(BaseModel):
    query: str = Field(..., description="The search query, as you would type it.")
    count: int | None = Field(
        None,
        description=f"How many results to return (default {DEFAULT_COUNT}, max {MAX_COUNT}).",
    )


class WebSearchTool(Tool[WebSearchInput]):
    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = (
        "Searches the web and returns a ranked list of results — title, URL and "
        "snippet — through the configured search provider (Brave by default; "
        "Serper, Tavily, SearXNG, Exa and Google Programmable Search are also "
        "supported). Use it to find pages worth reading, then pass the URLs to "
        "web_fetch for the actual content. Results are ranked by the provider, "
        f"not by QuickCode. Ask for {DEFAULT_COUNT} results unless you need more; "
        "every query costs quota. If no provider is configured the call fails "
        "with instructions rather than guessing — report that to the user instead "
        "of retrying."
    )
    is_read_only: ClassVar[bool] = False
    permission = PermissionSpec(mutates=True, target_field="query")
    Input = WebSearchInput

    def render_call(self, input: WebSearchInput) -> str:  # noqa: A002
        return f"⏺ Search: {input.query}"

    async def run(self, input: WebSearchInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        query = (input.query or "").strip()
        if not query:
            return ToolResult(content="Error: query is empty.", is_error=True)

        settings = _settings()
        count = input.count or (settings.max_results if settings else DEFAULT_COUNT)

        try:
            provider = resolve_provider(settings=settings)
        except SearchConfigError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)

        info = provider.info
        try:
            results = await run_search(provider, query, count=count)
        except SearchError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - a traceback could carry the key
            return ToolResult(
                content=f"Error: {info.label} search failed ({type(exc).__name__}).",
                is_error=True,
            )

        if not results:
            return ToolResult(
                content=f'No results from {info.label} for "{query}".',
                ui_meta={"provider": info.name, "query": query, "count": 0},
            )

        return ToolResult(
            content=_render(query, info.label, results),
            ui_meta={
                "provider": info.name,
                "provider_label": info.label,
                "query": query,
                "count": len(results),
                "results": [{"title": r.title, "url": r.url} for r in results],
            },
        )


def _settings():
    """The ``search`` block of the user's config, or None if it cannot be read."""
    try:
        from quickcode.config import Config

        return Config.load().search
    except Exception:  # noqa: BLE001 - a broken config must not hide the tool
        return None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _render(query: str, label: str, results: list[SearchResult]) -> str:
    """The hit list as one TOON table.

    Each hit used to cost a numbered line, an indented URL line and one or two
    more indented lines, with the field names implied by position. The table
    declares ``title,url,snippet`` once and spends one line per hit -- the
    largest saving of any result QuickCode renders, because the repetition was
    per hit rather than per file.

    The column set is decided once for the whole list, not per row: a table
    needs every row to carry the same fields, so ``extract`` is either present
    on all of them or on none.

    Snippets are prose and prose has commas, so nearly every cell here ends up
    quoted. ``toon.TAB`` would avoid that -- ``_clip`` collapses whitespace, so
    no value can contain a tab -- but it saves two characters a row and costs
    the one thing worth more: every result QuickCode renders, and the example
    in the system prompt, using the same delimiter.
    """
    with_extract = any(r.content and r.content != r.snippet for r in results)
    rows = []
    for result in results:
        row = {
            "title": result.title or result.url,
            "url": result.url,
            "snippet": _clip(result.snippet or "", SNIPPET_CHARS),
        }
        if with_extract:
            row["extract"] = _clip(result.content or "", CONTENT_CHARS)
        rows.append(row)
    return (
        f'Results for "{query}" via {label}:\n'
        + toon.fenced({"results": rows})
        + "\nUse web_fetch on a URL above to read the full page."
    )
