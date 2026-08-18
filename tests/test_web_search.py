"""web_search: every provider, the key resolution order, and the rate guard.

Each provider is exercised against ``httpx.MockTransport`` with a response body
shaped like the real one, so a parser change that stops normalizing correctly
fails here. No test makes a network request and no test uses a real key: the
keys below are obvious fakes and never leave the process.
"""

from __future__ import annotations

import httpx
import pytest

from quickcode.search import (
    PROVIDERS,
    Credentials,
    RateGuard,
    SearchConfigError,
    SearchError,
    SearchSettings,
    chosen_provider,
    configured_providers,
    resolve_credentials,
    resolve_provider,
    run_search,
    secret_name,
)
from quickcode.search.brave import BraveProvider
from quickcode.search.google_cse import GoogleCseProvider
from quickcode.search.searxng import SearxngProvider
from quickcode.search.tavily import TavilyProvider
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.web_search import WebSearchInput, WebSearchTool

FAKE_KEY = "test-not-a-real-key"

# Response bodies shaped like each vendor's documented output. Trimmed of the
# fields nothing reads, otherwise verbatim in structure.
BODIES: dict[str, dict] = {
    "brave": {
        "type": "search",
        "query": {"original": "quickcode"},
        "web": {
            "type": "search",
            "results": [
                {
                    "title": "QuickCode",
                    "url": "https://example.com/qc",
                    "description": "A local-first coding agent.",
                    "language": "en",
                    "family_friendly": True,
                }
            ],
        },
    },
    "serper": {
        "searchParameters": {"q": "quickcode", "type": "search", "engine": "google"},
        "organic": [
            {
                "title": "QuickCode",
                "link": "https://example.com/qc",
                "snippet": "A local-first coding agent.",
                "position": 1,
            }
        ],
        "credits": 1,
    },
    "tavily": {
        "query": "quickcode",
        "answer": None,
        "images": [],
        "results": [
            {
                "title": "QuickCode",
                "url": "https://example.com/qc",
                "content": "A local-first coding agent.",
                "score": 0.97,
                "raw_content": "QuickCode is a local-first coding agent with a web UI.",
            }
        ],
        "response_time": 1.2,
    },
    "searxng": {
        "query": "quickcode",
        "number_of_results": 1,
        "results": [
            {
                "url": "https://example.com/qc",
                "title": "QuickCode",
                "content": "A local-first coding agent.",
                "engine": "duckduckgo",
                "score": 1.0,
                "category": "general",
            }
        ],
        "answers": [],
        "suggestions": [],
    },
    "exa": {
        "requestId": "req_1",
        "resolvedSearchType": "neural",
        "results": [
            {
                "id": "https://example.com/qc",
                "title": "QuickCode",
                "url": "https://example.com/qc",
                "publishedDate": "2026-01-01",
                "author": None,
                "score": 0.42,
                "text": "A local-first coding agent.",
            }
        ],
        "costDollars": {"total": 0.005},
    },
    "google_cse": {
        "kind": "customsearch#search",
        "searchInformation": {"totalResults": "1"},
        "items": [
            {
                "kind": "customsearch#result",
                "title": "QuickCode",
                "htmlTitle": "<b>QuickCode</b>",
                "link": "https://example.com/qc",
                "displayLink": "example.com",
                "snippet": "A local-first coding agent.",
            }
        ],
    },
}


def make_provider(name: str):
    """A provider of this kind with fake, complete credentials."""
    info = PROVIDERS[name].info
    return PROVIDERS[name](
        Credentials(
            api_key=FAKE_KEY if info.needs_key else "",
            base_url="https://searx.example" if info.needs_base_url else info.default_base_url,
            extra={key: "cx-123" for key, _, _ in info.extra_fields},
        )
    )


def capture(name: str, status: int = 200):
    """A transport that records the request and answers with the canned body."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=BODIES[name])

    return httpx.MockTransport(handler), seen


def no_secrets(name: str) -> str | None:
    """A secret store with nothing in it, so no test can read a real key."""
    return None


# --------------------------------------------------------------------------
# Every provider normalizes to the same shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_every_provider_returns_the_shared_result_shape(name):
    transport, seen = capture(name)
    results = await run_search(
        make_provider(name), "quickcode", transport=transport, guard=RateGuard()
    )

    assert len(results) == 1
    assert results[0].title == "QuickCode"
    assert results[0].url == "https://example.com/qc"
    assert "local-first coding agent" in results[0].snippet
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("brave", "X-Subscription-Token"),
        ("serper", "X-API-KEY"),
        ("exa", "x-api-key"),
    ],
)
async def test_header_providers_send_the_key_in_its_own_header(name, where):
    transport, seen = capture(name)
    await run_search(make_provider(name), "q", transport=transport, guard=RateGuard())
    assert seen[0].headers[where] == FAKE_KEY


async def test_tavily_sends_a_bearer_token_and_never_a_key_in_the_body():
    transport, seen = capture("tavily")
    await run_search(make_provider("tavily"), "q", transport=transport, guard=RateGuard())
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in seen[0].content.decode()


async def test_tavily_carries_extracted_content_through():
    transport, _ = capture("tavily")
    results = await run_search(
        make_provider("tavily"), "q", transport=transport, guard=RateGuard()
    )
    assert results[0].content.startswith("QuickCode is a local-first")


async def test_searxng_needs_no_key_and_asks_for_json():
    provider = make_provider("searxng")
    assert provider.info.needs_key is False
    transport, seen = capture("searxng")
    await run_search(provider, "q", transport=transport, guard=RateGuard())
    assert seen[0].url.params["format"] == "json"
    assert seen[0].url.host == "searx.example"


async def test_google_cse_sends_the_engine_id():
    transport, seen = capture("google_cse")
    await run_search(
        make_provider("google_cse"), "q", count=12, transport=transport, guard=RateGuard()
    )
    assert seen[0].url.params["cx"] == "cx-123"
    assert seen[0].url.params["num"] == "10"  # google refuses more than ten


async def test_the_count_is_asked_for_and_enforced():
    transport, seen = capture("brave")
    results = await run_search(
        make_provider("brave"), "q", count=3, transport=transport, guard=RateGuard()
    )
    assert seen[0].url.params["count"] == "3"
    assert len(results) <= 3


# --------------------------------------------------------------------------
# Failure paths: loud, keyless, and never a fallback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "phrase"), [(401, "rejected"), (429, "rate limit")])
async def test_http_errors_become_readable_search_errors(status, phrase):
    transport, _ = capture("brave", status=status)
    with pytest.raises(SearchError, match=phrase):
        await run_search(make_provider("brave"), "q", transport=transport, guard=RateGuard())


async def test_an_error_never_repeats_the_key_even_when_it_is_in_the_url():
    """Google CSE puts the key in the query string; the message must not."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == FAKE_KEY  # it *is* sent
        return httpx.Response(500, text="boom")

    with pytest.raises(SearchError) as exc:
        await run_search(
            make_provider("google_cse"),
            "q",
            transport=httpx.MockTransport(handler),
            guard=RateGuard(),
        )
    assert FAKE_KEY not in str(exc.value)


async def test_a_transport_failure_names_the_host_not_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(SearchError) as exc:
        await run_search(
            make_provider("google_cse"),
            "q",
            transport=httpx.MockTransport(handler),
            guard=RateGuard(),
        )
    assert FAKE_KEY not in str(exc.value)
    assert "ConnectError" in str(exc.value)


async def test_a_non_json_body_fails_rather_than_being_guessed_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(SearchError, match="not JSON"):
        await run_search(
            make_provider("brave"),
            "q",
            transport=httpx.MockTransport(handler),
            guard=RateGuard(),
        )


async def test_an_unexpected_shape_degrades_instead_of_crashing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"web": {"results": [{"totally": "renamed"}]}})

    results = await run_search(
        make_provider("brave"),
        "q",
        transport=httpx.MockTransport(handler),
        guard=RateGuard(),
    )
    assert results == []


# --------------------------------------------------------------------------
# Rate guard
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_the_guard_spaces_queries_to_the_providers_limit():
    clock = FakeClock()
    guard = RateGuard(monotonic=clock.monotonic, sleep=clock.sleep)

    assert await guard.wait("brave", 1.0) == 0.0  # first query is free
    assert await guard.wait("brave", 1.0) == pytest.approx(1.0)
    clock.now += 0.4
    assert await guard.wait("brave", 1.0) == pytest.approx(0.6)
    assert clock.slept == pytest.approx([1.0, 0.6])


async def test_the_guard_is_per_provider():
    clock = FakeClock()
    guard = RateGuard(monotonic=clock.monotonic, sleep=clock.sleep)
    await guard.wait("brave", 1.0)
    assert await guard.wait("serper", 1.0) == 0.0
    assert clock.slept == []


async def test_run_search_uses_the_guard():
    clock = FakeClock()
    guard = RateGuard(monotonic=clock.monotonic, sleep=clock.sleep)
    transport, _ = capture("brave")
    provider = make_provider("brave")

    await run_search(provider, "one", transport=transport, guard=guard)
    await run_search(provider, "two", transport=transport, guard=guard)

    # Brave's free tier is one query a second, so the second one waited.
    assert clock.slept == [pytest.approx(1.0)]


# --------------------------------------------------------------------------
# Provider choice and key resolution
# --------------------------------------------------------------------------


def test_the_default_provider_is_brave():
    assert chosen_provider(SearchSettings(), {}) == "brave"


def test_config_beats_env_for_the_provider_choice():
    settings = SearchSettings(provider="serper")
    assert chosen_provider(settings, {"QUICKCODE_SEARCH_PROVIDER": "tavily"}) == "serper"
    assert chosen_provider(SearchSettings(), {"QUICKCODE_SEARCH_PROVIDER": "tavily"}) == "tavily"


def test_key_resolution_order_is_config_then_env_then_store():
    info = BraveProvider.info
    settings = SearchSettings(providers={"brave": {"api_key": "from-config"}})
    env = {info.api_key_env: "from-env"}

    creds, missing = resolve_credentials(info, settings, env, lambda name: "from-store")
    assert (creds.api_key, missing) == ("from-config", [])

    creds, _ = resolve_credentials(info, SearchSettings(), env, lambda name: "from-store")
    assert creds.api_key == "from-env"

    creds, _ = resolve_credentials(info, SearchSettings(), {}, lambda name: "from-store")
    assert creds.api_key == "from-store"


def test_the_store_is_asked_under_a_namespaced_name():
    asked: list[str] = []

    def load(name: str) -> str | None:
        asked.append(name)
        return None

    resolve_credentials(BraveProvider.info, SearchSettings(), {}, load)
    assert asked == [secret_name("brave")] == ["search-brave"]


def test_searxng_resolves_a_base_url_instead_of_a_key():
    info = SearxngProvider.info
    creds, missing = resolve_credentials(info, SearchSettings(), {}, no_secrets)
    assert missing == ["the base URL of the instance to query"]

    creds, missing = resolve_credentials(
        info, SearchSettings(), {info.base_url_env: "http://localhost:8080"}, no_secrets
    )
    assert (creds.base_url, missing) == ("http://localhost:8080", [])


def test_google_cse_wants_both_a_key_and_an_engine_id():
    info = GoogleCseProvider.info
    _, missing = resolve_credentials(
        info, SearchSettings(), {info.api_key_env: FAKE_KEY}, no_secrets
    )
    assert len(missing) == 1 and "engine id" in missing[0]


def test_resolve_returns_the_configured_provider():
    settings = SearchSettings(provider="tavily", providers={"tavily": {"api_key": FAKE_KEY}})
    provider = resolve_provider(settings=settings, env={}, load=no_secrets)
    assert isinstance(provider, TavilyProvider)


def test_an_unknown_provider_is_named_and_refused():
    with pytest.raises(SearchConfigError, match="unknown search provider"):
        resolve_provider(settings=SearchSettings(provider="bing"), env={}, load=no_secrets)


def test_a_missing_key_names_the_env_var_and_the_signup_page():
    with pytest.raises(SearchConfigError) as exc:
        resolve_provider(settings=SearchSettings(), env={}, load=no_secrets)
    message = str(exc.value)
    assert "QUICKCODE_BRAVE_API_KEY" in message
    assert BraveProvider.info.signup_url in message
    assert "set-key brave" in message


def test_a_ready_alternative_is_named_but_never_used():
    """Brave is chosen and unconfigured; Serper is ready. It must not switch."""
    settings = SearchSettings(providers={"serper": {"api_key": FAKE_KEY}})
    with pytest.raises(SearchConfigError) as exc:
        resolve_provider(settings=settings, env={}, load=no_secrets)
    message = str(exc.value)
    assert "Serper" in message
    assert "will not switch" in message
    assert configured_providers(settings, {}, no_secrets) == ["serper"]


def test_every_provider_declares_where_to_get_credentials():
    for name, cls in PROVIDERS.items():
        info = cls.info
        assert info.signup_url.startswith("https://"), name
        assert info.needs_key or info.needs_base_url, name
        if info.needs_key:
            assert info.api_key_env.startswith("QUICKCODE_"), name


# --------------------------------------------------------------------------
# The encrypted store
# --------------------------------------------------------------------------


def test_a_search_key_round_trips_through_the_encrypted_store(tmp_path, monkeypatch):
    from quickcode import secrets

    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path)
    secrets.save_secret("search-brave", FAKE_KEY)

    assert secrets.has_secret("search-brave")
    assert secrets.load_secret("search-brave") == FAKE_KEY
    # Stored encrypted (DPAPI on Windows, base64 behind a 0600 file elsewhere).
    assert FAKE_KEY.encode() not in (tmp_path / "search-brave.key").read_bytes()

    secrets.clear_secret("search-brave")
    assert not secrets.has_secret("search-brave")


def test_a_secret_name_cannot_become_a_path(tmp_path, monkeypatch):
    from quickcode import secrets

    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path)
    for bad in ("../escape", "a/b", "C:\\keys", ""):
        with pytest.raises(ValueError):
            secrets.secret_path(bad)
        assert secrets.load_secret(bad) is None


def test_the_openrouter_key_still_has_its_own_path(tmp_path, monkeypatch):
    from quickcode import secrets

    monkeypatch.setattr(secrets, "_SECRET_PATH", tmp_path / "openrouter.key")
    secrets.save_api_key("or-fake")
    assert secrets.load_api_key() == "or-fake"
    assert secrets.has_saved_key()


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


def ctx(tmp_path) -> ToolCtx:
    return ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())


def use_settings(monkeypatch, settings: SearchSettings, env: dict[str, str] | None = None):
    """Pin the tool to test settings so it never reads the real config."""
    monkeypatch.setattr("quickcode.tools.web_search._settings", lambda: settings)
    monkeypatch.setattr(
        "quickcode.tools.web_search.resolve_provider",
        lambda settings=None: resolve_provider(
            settings=settings, env=env or {}, load=no_secrets
        ),
    )


async def test_tool_renders_a_ranked_list(tmp_path, monkeypatch):
    settings = SearchSettings(provider="brave", providers={"brave": {"api_key": FAKE_KEY}})
    use_settings(monkeypatch, settings)
    transport, _ = capture("brave")
    monkeypatch.setattr(
        "quickcode.tools.web_search.run_search",
        lambda provider, query, count=5: run_search(
            provider, query, count=count, transport=transport, guard=RateGuard()
        ),
    )

    result = await WebSearchTool().run(WebSearchInput(query="quickcode"), ctx(tmp_path))

    assert not result.is_error
    assert "1. QuickCode" in result.content
    assert "https://example.com/qc" in result.content
    assert "via Brave Search" in result.content
    assert result.ui_meta["provider"] == "brave"
    assert FAKE_KEY not in result.content
    assert FAKE_KEY not in str(result.ui_meta)


async def test_tool_explains_an_unconfigured_provider_instead_of_failing_blankly(
    tmp_path, monkeypatch
):
    use_settings(monkeypatch, SearchSettings())
    result = await WebSearchTool().run(WebSearchInput(query="anything"), ctx(tmp_path))

    assert result.is_error
    assert "QUICKCODE_BRAVE_API_KEY" in result.content
    assert "api-dashboard.search.brave.com" in result.content


async def test_tool_rejects_an_empty_query(tmp_path, monkeypatch):
    use_settings(monkeypatch, SearchSettings())
    result = await WebSearchTool().run(WebSearchInput(query="   "), ctx(tmp_path))
    assert result.is_error and "empty" in result.content


def test_the_model_cannot_choose_the_provider():
    schema = WebSearchTool().schema()
    assert set(schema.parameters["properties"]) == {"query", "count"}
    assert schema.parameters["additionalProperties"] is False


def test_the_provider_layer_is_extensible_without_touching_the_tool():
    """Every shipped provider satisfies the one interface the tool depends on."""
    for name in PROVIDERS:
        provider = make_provider(name)
        assert hasattr(provider, "build_request") and hasattr(provider, "parse"), name
        request = provider.build_request("q", 5)
        assert isinstance(request, httpx.Request)
        assert request.url.scheme in ("http", "https")
        assert provider.parse(BODIES[name])[0].url == "https://example.com/qc"
