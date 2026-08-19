"""The balance, and the four ways there isn't one to show.

Written after a run stopped on `402 Insufficient credits`: the number that
decided whether the next request would work was the one number the app could
not show. Every case here answers with the same shape, because a status line
with four branches is a status line that gets one of them wrong.

No network: every provider answer is a stub transport.
"""

from __future__ import annotations

import httpx
import pytest

from quickcode.providers import credits

OPENROUTER = "https://openrouter.ai/api/v1"


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_the_balance_is_what_is_left_not_what_was_bought() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/credits")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": {"total_credits": 10.0, "total_usage": 7.25}})

    out = await credits.fetch(OPENROUTER, "sk-test", transport=transport(handler))
    assert out["supported"] is True
    assert out["available"] == pytest.approx(2.75)
    assert out["total"] == pytest.approx(10.0)
    assert out["used"] == pytest.approx(7.25)
    assert out["error"] == ""


async def test_a_provider_that_does_not_publish_a_balance_says_so() -> None:
    out = await credits.fetch("https://api.example.com/v1", "sk-test")
    assert out["supported"] is False
    assert out["available"] is None
    assert out["error"]


async def test_no_key_means_no_lookup_rather_than_a_failed_one() -> None:
    out = await credits.fetch(OPENROUTER, "")
    assert out["available"] is None
    assert "key" in out["error"]


@pytest.mark.parametrize("answer", [
    httpx.Response(401, json={"error": "bad key"}),
    httpx.Response(200, json={"nonsense": True}),
    httpx.Response(200, json={"data": {"total_credits": "n/a"}}),
])
async def test_a_provider_that_answers_badly_never_breaks_the_caller(answer) -> None:
    """This decorates a status bar. It must not turn into a failed request."""
    out = await credits.fetch(OPENROUTER, "sk-test", transport=transport(lambda r: answer))
    assert out["available"] is None
    assert out["error"]
    assert out["supported"] is True


async def test_an_unreachable_provider_is_reported_not_raised() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    out = await credits.fetch(OPENROUTER, "sk-test", transport=transport(boom))
    assert out["available"] is None
    assert out["error"] == "could not reach the provider"
