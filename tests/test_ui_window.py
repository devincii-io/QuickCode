"""Native window helpers: WebView2 detection and single-instance hand-off."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from quickcode import webapp
from quickcode.ui import window


def test_webview2_check_is_true_off_windows(monkeypatch):
    """The registry probe is Windows/EdgeChromium-specific; elsewhere it's a no-op."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert window._webview2_runtime_present() is True


def test_focus_existing_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert window.focus_existing() is False


def test_available_false_without_pywebview(monkeypatch):
    """Missing pywebview (or its GUI toolkit) must fail closed to the browser
    fallback, not raise -- exactly the pre-existing contract in webapp.py."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no webview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert window.available() is False


@pytest.fixture
def impatient_probes(monkeypatch):
    """Shrink the two single-instance probe budgets for the unreachable tests.

    "Nothing is listening" is not a fast refusal here: a host firewall that
    drops loopback SYNs instead of rejecting them makes the connect sit for the
    whole timeout, so these two tests used to spend 2.6s between them doing
    nothing. What they actually assert is the *shape* of the failure -- probe
    returns None, hand-off returns False rather than raising -- and that is
    identical whether the budget is two seconds or fifty milliseconds.
    """
    monkeypatch.setattr(webapp, "HEALTH_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(webapp, "HAND_OFF_TIMEOUT_S", 0.05)


def test_running_instance_health_none_when_unreachable(impatient_probes):
    # Nothing is listening on this high port during the test run.
    assert webapp._running_instance_health(58643) is None


def test_hand_off_returns_false_when_unreachable(tmp_path, impatient_probes):
    assert webapp._hand_off_to_running_instance(58643, tmp_path) is False


# ---- handing a project to a running instance ----
#
# The hand-off request has to carry the runtime token, and anything can bind a
# free loopback port. So the token only goes to an instance that proves it
# already has it, and never through a proxy.


class _Fake:
    """A loopback HTTP server that records what it is sent."""

    def __init__(self, answer_health) -> None:
        import http.server
        import threading

        fake = self
        self.seen: list[tuple[str, str, dict[str, str], bytes]] = []
        self.answer_health = answer_health

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.seen.append((self.command, self.path, dict(self.headers), body))
                return body

            def do_GET(self):  # noqa: N802 - http.server's naming
                self._record()
                self._reply(200, json.dumps(fake.answer_health(self.path)).encode())

            def do_POST(self):  # noqa: N802
                self._record()
                self._reply(200, b'{"id": "abc"}')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def carried_token(self, token: str) -> bool:
        return any(token in str(headers) or token.encode() in body
                   for _m, _p, headers, body in self.seen)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes():
    made: list[_Fake] = []

    def make(answer_health):
        made.append(_Fake(answer_health))
        return made[-1]

    yield make
    for fake in made:
        fake.close()


def _challenge(path: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(path).query).get("challenge", [""])[0]


def _honest(token: str):
    """A health answer that proves the token for whichever port it is on."""
    from quickcode.server import auth

    holder: dict[str, int] = {}

    def answer(path: str) -> dict:
        return {"app": "quickcode",
                "proof": auth.instance_proof(token, holder["port"], _challenge(path))}

    return holder, answer


def test_a_squatter_on_the_port_is_never_handed_the_token(tmp_path, fakes):
    from quickcode.server import auth

    token = auth.get_or_create_token()
    squatter = fakes(lambda path: {"app": "quickcode", "version": "9.9.9", "proof": "0" * 64})

    assert webapp._running_instance_health(squatter.port) is None
    assert webapp._hand_off_to_running_instance(squatter.port, tmp_path) is False
    assert not squatter.carried_token(token)
    assert all(m == "GET" for m, *_ in squatter.seen), "a request went past the proof"


def test_a_proof_relayed_from_another_port_is_not_accepted(tmp_path, fakes):
    """A squatter that forwards the challenge to a real instance elsewhere
    gets back a proof for *that* port, not the one the launcher asked."""
    from quickcode.server import auth

    token = auth.get_or_create_token()
    relay = fakes(lambda path: {
        "app": "quickcode", "proof": auth.instance_proof(token, 1, _challenge(path)),
    })

    assert webapp._hand_off_to_running_instance(relay.port, tmp_path) is False
    assert not relay.carried_token(token)


def test_an_instance_that_proves_it_holds_the_token_gets_the_project(tmp_path, fakes):
    from quickcode.server import auth

    token = auth.get_or_create_token()
    holder, answer = _honest(token)
    fake = fakes(answer)
    holder["port"] = fake.port

    assert webapp._running_instance_health(fake.port)["app"] == "quickcode"
    assert webapp._hand_off_to_running_instance(fake.port, tmp_path) is True
    post = [s for s in fake.seen if s[0] == "POST"]
    assert len(post) == 1
    _m, path, headers, body = post[0]
    assert path == "/api/projects/open"
    assert json.loads(body) == {"path": str(tmp_path)}
    assert {k.lower(): v for k, v in headers.items()}[auth.HEADER] == token


def test_the_hand_off_never_goes_through_a_proxy(tmp_path, fakes, monkeypatch):
    """urllib honours HTTP_PROXY even for 127.0.0.1 unless NO_PROXY names it
    (and on Windows it reads the system proxy from the registry), which would
    have sent the token to the proxy."""
    from quickcode.server import auth

    token = auth.get_or_create_token()
    proxy = fakes(lambda path: {})
    holder, answer = _honest(token)
    fake = fakes(answer)
    holder["port"] = fake.port
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{proxy.port}")
    # urlopen caches its opener, proxies and all, on first use.
    import urllib.request

    monkeypatch.setattr(urllib.request, "_opener", None)

    assert webapp._hand_off_to_running_instance(fake.port, tmp_path) is True
    assert proxy.seen == []


def test_the_hand_off_reaches_a_real_server_and_opens_the_project(tmp_path):
    """The fake above answers the way the tests say it should; this is the
    route and the launcher agreeing over a real socket, keep-alive and all."""
    import threading
    import time

    import uvicorn

    from quickcode.server import auth
    from quickcode.server.app import create_app
    from quickcode.server.projects import ProjectHub, project_id
    from tests.test_server import FakeProvider, make_manager

    (tmp_path / "home").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    hub = ProjectHub.from_manager(make_manager(tmp_path / "home", FakeProvider([])))
    port = webapp._free_port()
    app = create_app(hub, port=port, token=auth.get_or_create_token())
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
        loop="asyncio", http="h11", ws="websockets-sansio",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        assert webapp._hand_off_to_running_instance(port, other) is True
        assert hub.get(project_id(other)) is not None
    finally:
        server.should_exit = True
        thread.join(10)


def test_a_request_that_never_finishes_cannot_hold_shutdown_open(monkeypatch):
    """The hub -- terminals, MCP servers -- is only closed after uvicorn
    returns, and uvicorn waited for every open request without limit. The
    window gives the server thread a fixed time to unwind, so one slow
    request used to be enough to skip the cleanup entirely."""
    import threading
    import time
    import urllib.request

    import uvicorn
    from fastapi import FastAPI

    monkeypatch.setattr(webapp, "SHUTDOWN_GRACE_S", 0.3)
    started = threading.Event()
    app = FastAPI()

    @app.get("/stuck")
    async def stuck():
        started.set()
        await asyncio.sleep(3600)

    port = webapp._free_port()
    server = uvicorn.Server(webapp._server_config(app, port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request() -> None:
        try:
            opener.open(f"http://127.0.0.1:{port}/stuck", timeout=30)
        except Exception:
            pass  # cut off by the shutdown, which is the point

    threading.Thread(target=request, daemon=True).start()
    assert started.wait(5)

    t0 = time.monotonic()
    server.should_exit = True
    thread.join(5)
    assert not thread.is_alive(), "shutdown waited on the stuck request"
    assert time.monotonic() - t0 < 3


def test_the_console_line_never_carries_the_token_when_the_app_opens_its_own_window():
    url = "http://127.0.0.1:8642/#token=secret-value&project=abc"
    assert "secret-value" not in webapp._printable(url, opened=True)
    assert webapp._printable(url, opened=False) == url
