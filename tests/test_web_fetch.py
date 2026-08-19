"""web_fetch: what it refuses, what it caps, and what it hands the model.

Every test here runs against ``httpx.MockTransport`` and a stub resolver.
Nothing in this file makes a network request or a DNS query -- which is also
the only honest way to test an SSRF guard, since the interesting cases are
addresses that must never be connected to.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.web_fetch import WebFetchInput, WebFetchTool
from quickcode.web import fetch as fetch_mod
from quickcode.web.fetch import FetchError, build_request, fetch_url, user_agent
from quickcode.web.markdown import html_to_markdown
from quickcode.web.ssrf import BlockedURL, classify_host, classify_ip, validate_url

PUBLIC_IP = "93.184.216.34"


def resolver(mapping: dict[str, list[str]]):
    """A stub DNS: name -> addresses, with a public default."""

    async def resolve(host: str, port: int) -> list[str]:
        return mapping.get(host, [PUBLIC_IP])

    return resolve


def ctx(tmp_path) -> ToolCtx:
    return ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())


# --------------------------------------------------------------------------
# Address and hostname classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",          # QuickCode's own API listens here
        "127.1.1.1",
        "::1",
        "0.0.0.0",
        "::",
        "10.0.0.7",           # RFC1918
        "172.16.4.4",
        "192.168.1.1",
        "169.254.169.254",    # cloud metadata
        "fe80::1",            # IPv6 link-local
        "fc00::1",            # IPv6 unique-local
        "fd00::abcd",
        "224.0.0.1",          # multicast
        "ff02::1",
        "240.0.0.1",          # reserved
        "100.64.0.1",         # carrier-grade NAT
        "::ffff:127.0.0.1",   # IPv4-mapped loopback
        "::ffff:10.0.0.1",
        "2002:7f00:1::",      # 6to4 wrapping 127.0.0.1
    ],
)
def test_non_public_addresses_are_refused(address):
    assert classify_ip(address), f"{address} should not be reachable"


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_pass(address):
    assert classify_ip(address) == ""


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "printer.local",
        "wiki.internal",
        "nas.lan",
        "thing.home.arpa",
        "intranet",          # bare name, resolved through search domains
        "127.0.0.1",
        "[::1]".strip("[]"),
    ],
)
def test_non_public_hostnames_are_refused(host):
    assert classify_host(host)


def test_public_hostname_passes():
    assert classify_host("example.com") == ""
    assert classify_host("EXAMPLE.COM.") == ""  # case and trailing dot


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/x",
        "data:text/html,<b>hi</b>",
        "gopher://example.com/",
        "jar:http://example.com!/",
    ],
)
async def test_only_http_schemes_are_fetchable(url):
    with pytest.raises(BlockedURL):
        await validate_url(url, resolve=resolver({}))


async def test_credentials_in_the_url_are_refused():
    with pytest.raises(BlockedURL, match="credentials"):
        await validate_url("https://user:pw@example.com/", resolve=resolver({}))


async def test_a_public_name_resolving_to_loopback_is_refused():
    """The rebinding case: the name looks fine, the answer does not."""
    with pytest.raises(BlockedURL, match="127.0.0.1"):
        await validate_url(
            "https://evil.example/", resolve=resolver({"evil.example": ["127.0.0.1"]})
        )


async def test_one_bad_address_refuses_the_whole_name():
    """A host answering with a good *and* a bad address is not a good host."""
    with pytest.raises(BlockedURL):
        await validate_url(
            "https://mixed.example/",
            resolve=resolver({"mixed.example": [PUBLIC_IP, "10.1.2.3"]}),
        )


async def test_validated_target_pins_the_checked_address():
    target = await validate_url("https://example.com/docs?q=1", resolve=resolver({}))
    assert target.ip == PUBLIC_IP
    assert target.host == "example.com"
    assert target.header_host == "example.com"  # default port omitted
    assert target.pinned_url() == f"https://{PUBLIC_IP}:443/docs?q=1"


async def test_non_default_port_is_kept_in_the_host_header():
    target = await validate_url("http://example.com:8080/a", resolve=resolver({}))
    assert target.header_host == "example.com:8080"


async def test_request_is_addressed_to_the_ip_and_named_by_the_host():
    target = await validate_url("https://example.com/x", resolve=resolver({}))
    request = build_request(target)
    assert request.url.host == PUBLIC_IP
    assert request.headers["Host"] == "example.com"
    # SNI (and therefore certificate verification) still uses the real name.
    assert request.extensions["sni_hostname"] == "example.com"
    assert "QuickCode" in request.headers["User-Agent"]
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers


def test_user_agent_is_truthful():
    agent = user_agent()
    assert agent.startswith("QuickCode/")
    assert "github.com" in agent


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

PAGE = """
<html><head><title>Guide</title><style>.a{color:red}</style></head>
<body>
  <nav><a href="/elsewhere">Menu</a></nav>
  <h1>Install</h1>
  <p>Run the <code>setup</code> script, then read
     <a href="/next">the next page</a>.</p>
  <ul><li>one</li><li>two</li></ul>
  <pre>pip install quickcode</pre>
  <script>alert(1)</script>
  <footer>copyright</footer>
</body></html>
"""


def html_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, html=PAGE)


async def test_a_page_comes_back_as_markdown():
    outcome = await fetch_url(
        "https://example.com/guide",
        transport=httpx.MockTransport(html_response),
        resolve=resolver({}),
    )
    title, markdown = html_to_markdown(outcome.body, base_url=outcome.final_url)

    assert outcome.status == 200
    assert title == "Guide"
    assert "# Install" in markdown
    assert "- one" in markdown
    assert "`setup`" in markdown
    assert "[the next page](https://example.com/next)" in markdown
    assert "pip install quickcode" in markdown
    # Chrome and scripts are gone.
    assert "alert(1)" not in markdown
    assert "Menu" not in markdown
    assert "copyright" not in markdown


async def test_redirect_to_loopback_is_refused():
    """A public URL that 302s to the local API is the standard bypass."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/api/sessions"})

    with pytest.raises(FetchError, match="loopback"):
        await fetch_url(
            "https://example.com/redirect",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
        )


async def test_redirect_to_a_name_that_resolves_privately_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://inside.example/secrets"})

    with pytest.raises(FetchError, match="10.0.0.5"):
        await fetch_url(
            "https://example.com/go",
            transport=httpx.MockTransport(handler),
            resolve=resolver({"inside.example": ["10.0.0.5"]}),
        )


async def test_a_good_redirect_is_followed_and_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Host"] == "example.com":
            return httpx.Response(302, headers={"location": "https://docs.example.com/final"})
        return httpx.Response(200, text="arrived", headers={"content-type": "text/plain"})

    outcome = await fetch_url(
        "https://example.com/start",
        transport=httpx.MockTransport(handler),
        resolve=resolver({}),
    )
    assert outcome.body == "arrived"
    assert outcome.final_url == "https://docs.example.com/final"
    assert outcome.redirects == ["https://example.com/start"]


async def test_redirect_to_another_scheme_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///C:/Windows/win.ini"})

    with pytest.raises(FetchError, match="file: scheme"):
        await fetch_url(
            "https://example.com/go",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
        )


async def test_a_refusal_after_a_redirect_says_so():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://192.168.0.1/admin"})

    with pytest.raises(FetchError, match="after a redirect"):
        await fetch_url(
            "https://example.com/go",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
        )


async def test_a_cookie_set_on_one_hop_is_not_replayed_on_the_next():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/first":
            return httpx.Response(
                302,
                headers={
                    "location": "https://example.com/second",
                    "set-cookie": "session=secret; Path=/",
                },
            )
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    await fetch_url(
        "https://example.com/first",
        transport=httpx.MockTransport(handler),
        resolve=resolver({}),
    )
    assert len(seen) == 2
    assert "cookie" not in seen[1].headers


async def test_redirect_loops_are_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    with pytest.raises(FetchError, match="redirects"):
        await fetch_url(
            "https://example.com/loop",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
            max_redirects=3,
        )


async def test_a_declared_oversize_body_is_refused_without_downloading():
    downloaded = []

    def handler(request: httpx.Request) -> httpx.Response:
        downloaded.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "content-length": "999999999"},
            content=b"x",
        )

    with pytest.raises(FetchError, match="Nothing was downloaded"):
        await fetch_url(
            "https://example.com/huge",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
            max_bytes=1000,
        )
    assert len(downloaded) == 1


async def test_an_undeclared_oversize_body_is_cut_off_mid_stream():
    """No content-length, so the cap has to bite while reading."""
    chunks_sent = 0

    async def body():
        nonlocal chunks_sent
        for _ in range(1000):
            chunks_sent += 1
            yield b"a" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body())

    outcome = await fetch_url(
        "https://example.com/stream",
        transport=httpx.MockTransport(handler),
        resolve=resolver({}),
        max_bytes=4096,
    )
    assert outcome.truncated
    assert outcome.bytes_read == 4096
    # The generator was abandoned rather than drained into memory.
    assert chunks_sent <= 5


async def test_binary_content_is_refused_by_type():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")

    with pytest.raises(FetchError, match="image/png"):
        await fetch_url(
            "https://example.com/x.png",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
        )


async def test_http_errors_are_reported_not_returned_as_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope", headers={"content-type": "text/plain"})

    with pytest.raises(FetchError, match="404"):
        await fetch_url(
            "https://example.com/missing",
            transport=httpx.MockTransport(handler),
            resolve=resolver({}),
        )


async def test_the_whole_fetch_is_time_capped(monkeypatch):
    """A transport that never answers must be cut off by the cap, not waited on.

    `MIN_TIMEOUT_S` is lowered rather than passing a small `timeout_s`, because
    the production floor would clamp anything below a second straight back up
    and the test would then spend that second proving it. What is under test is
    the cap firing at all, and that is the same mechanism at 20ms as at 1s.
    """
    monkeypatch.setattr(fetch_mod, "MIN_TIMEOUT_S", 0.02)

    async def never_answers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, text="late")

    with pytest.raises(FetchError, match="timed out"):
        await fetch_url(
            "https://example.com/slow",
            transport=httpx.MockTransport(never_answers),
            resolve=resolver({}),
            timeout_s=0.02,
        )


# --------------------------------------------------------------------------
# Markdown conversion
# --------------------------------------------------------------------------


def test_markdown_keeps_structure_and_drops_chrome():
    title, out = html_to_markdown(
        "<title>T</title><h2>Head</h2><p>Body <b>bold</b>.</p>"
        "<ol><li>first</li><li>second</li></ol>"
        "<blockquote>quoted</blockquote>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<aside>sidebar</aside>",
        base_url="https://example.com/docs/",
    )
    assert title == "T"
    assert "## Head" in out
    assert "**bold**" in out
    assert "1. first" in out and "2. second" in out
    assert "> quoted" in out
    assert "| a | b |" in out and "| --- | --- |" in out
    assert "sidebar" not in out


def test_markdown_resolves_relative_links():
    _, out = html_to_markdown(
        '<a href="../up.html">up</a>', base_url="https://example.com/a/b/"
    )
    assert "[up](https://example.com/a/up.html)" in out


def test_markdown_survives_broken_html():
    _, out = html_to_markdown("<p>unclosed <b>bold <ul><li>x")
    assert "unclosed" in out


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------


async def test_tool_renders_markdown_with_provenance(tmp_path, monkeypatch):
    from quickcode.web.fetch import FetchOutcome

    async def fake_fetch(url, **kwargs):
        return FetchOutcome(
            url=url,
            final_url="https://example.com/guide",
            status=200,
            content_type="text/html; charset=utf-8",
            body=PAGE,
            bytes_read=len(PAGE),
        )

    monkeypatch.setattr("quickcode.tools.web_fetch.fetch_url", fake_fetch)
    result = await WebFetchTool().run(
        WebFetchInput(url="https://example.com/guide"), ctx(tmp_path)
    )

    assert not result.is_error
    assert "# Guide" in result.content
    assert 'url="https://example.com/guide"' in result.content
    assert "# Install" in result.content
    assert result.ui_meta["status"] == 200
    assert result.ui_meta["title"] == "Guide"


async def test_tool_marks_its_own_truncation(tmp_path, monkeypatch):
    from quickcode.web.fetch import FetchOutcome

    async def fake_fetch(url, **kwargs):
        return FetchOutcome(
            url=url, final_url=url, status=200, content_type="text/plain",
            body="x" * 5000, bytes_read=5000,
        )

    monkeypatch.setattr("quickcode.tools.web_fetch.fetch_url", fake_fetch)
    result = await WebFetchTool().run(
        WebFetchInput(url="https://example.com/big", max_chars=100), ctx(tmp_path)
    )
    assert "<truncated" in result.content
    assert result.ui_meta["truncated"] is True


async def test_tool_reports_a_refusal_as_an_error(tmp_path):
    result = await WebFetchTool().run(
        WebFetchInput(url="file:///C:/Windows/win.ini"), ctx(tmp_path)
    )
    assert result.is_error
    assert "file: scheme" in result.content


async def test_tool_refuses_the_local_api_without_asking_dns(tmp_path):
    result = await WebFetchTool().run(
        WebFetchInput(url="http://127.0.0.1:8765/api/sessions"), ctx(tmp_path)
    )
    assert result.is_error
    assert "loopback" in result.content


# --------------------------------------------------------------------------
# Registration and gating: the web tools are subject to the same engine
# --------------------------------------------------------------------------


def test_both_tools_are_registered():
    from quickcode.tools.registry import default_registry

    tools = default_registry().tools
    assert "web_fetch" in tools and "web_search" in tools


def test_they_prompt_in_ask_mode_and_match_on_their_own_target(tmp_path):
    from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
    from quickcode.tools.registry import default_registry

    engine = PermissionEngine(mode=Mode.ask, rules=Rules(), root=tmp_path)
    fetch = default_registry().get("web_fetch")

    decision, target = engine.evaluate_tool(fetch, {"url": "https://example.com/a"})
    assert (decision, target) == (Decision.ask, "https://example.com/a")
    assert engine.suggest_rule("web_fetch", "https://example.com/a")


def test_a_rule_can_allow_one_site_and_deny_another(tmp_path):
    from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules

    engine = PermissionEngine(
        mode=Mode.ask,
        rules=Rules(allow=["web_fetch(https://docs.example.com/**)"],
                    deny=["web_fetch(http://**)"]),
        root=tmp_path,
    )
    assert engine.evaluate("web_fetch", "https://docs.example.com/a/b") == Decision.allow
    assert engine.evaluate("web_fetch", "http://docs.example.com/a") == Decision.deny
    assert engine.evaluate("web_fetch", "https://elsewhere.com/a") == Decision.ask


def test_plan_mode_withholds_them_like_any_other_mutating_tool():
    from quickcode.core.hooks import PlanModeHook
    from quickcode.core.permissions import Mode
    from quickcode.tools.registry import core_tools

    class FakeAgent:
        mode = Mode.plan

    visible = {t.name for t in PlanModeHook().visible_tools(FakeAgent(), core_tools())}
    assert "web_fetch" not in visible and "web_search" not in visible


def test_a_subagents_tool_list_can_withhold_them():
    from quickcode.tools.registry import build_registry

    narrow = build_registry(["read", "grep"])
    assert "web_fetch" not in narrow.tools and "web_search" not in narrow.tools

    granted = build_registry(["read", "web_search"])
    assert set(granted.tools) == {"read", "web_search"}

    inherited = build_registry(None)
    assert "web_fetch" in inherited.tools
