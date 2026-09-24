"""The loopback trust boundary, probed from the outside.

The API answers QuickCode's own window and nothing else: a Host allowlist
against DNS rebinding, an Origin allowlist against other sites in the same
browser, and the runtime token against other local processes. Each test here
is one way in that must stay shut, driven through the real app.
"""

from __future__ import annotations

import re
import secrets

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quickcode.server import auth
from quickcode.server.app import FRONTEND_DIR, create_app
from tests.test_server import FakeProvider, make_manager

TOKEN = "t" * 43
HOST = "127.0.0.1:8642"


@pytest.fixture
def client(tmp_path):
    app = create_app(make_manager(tmp_path, FakeProvider([])), port=8642, token=TOKEN)
    with TestClient(app, base_url=f"http://{HOST}") as c:
        yield c


def _get(client, path="/api/bootstrap", **headers):
    return client.get(path, headers={auth.HEADER: TOKEN, **headers})


# ---- Host: DNS rebinding ----


@pytest.mark.parametrize("host", [
    "evil.example:8642",
    "127.0.0.1.evil.example:8642",
    "localhost.:8642",
    "127.0.0.1:8643",
    "127.0.0.1",
    "",
])
def test_a_host_that_is_not_this_server_is_refused_even_with_the_token(client, host):
    assert _get(client, host=host).status_code == 403


@pytest.mark.parametrize("host", [HOST, "localhost:8642", "[::1]:8642"])
def test_every_loopback_spelling_of_this_server_is_accepted(client, host):
    assert _get(client, host=host).status_code == 200


# ---- Origin: other sites in the same browser ----


@pytest.mark.parametrize("origin", [
    "null", "", "http://evil.example", "http://127.0.0.1:8643",
    "https://127.0.0.1:8642", f"http://{HOST}.evil.example",
])
def test_a_foreign_or_opaque_origin_is_refused_even_with_the_token(client, origin):
    assert _get(client, origin=origin).status_code == 403


def test_a_same_origin_or_originless_request_with_the_token_is_accepted(client):
    assert _get(client, origin=f"http://{HOST}").status_code == 200
    assert _get(client).status_code == 200


def test_a_cross_origin_preflight_is_refused_and_grants_nothing(client):
    r = client.options("/api/bootstrap", headers={
        "origin": "http://evil.example",
        "access-control-request-method": "GET",
        "access-control-request-headers": auth.HEADER,
    })
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


# ---- the token ----


@pytest.mark.parametrize("offered", [
    "", "t" * 42, "t" * 44, "T" * 43, " " + "t" * 43, "t" * 42 + "u",
])
def test_anything_but_the_exact_token_is_refused(client, offered):
    assert client.get("/api/bootstrap", headers={auth.HEADER: offered}).status_code == 403


def test_a_token_header_that_is_not_ascii_is_refused_rather_than_crashing(client):
    r = client.get("/api/bootstrap", headers={auth.HEADER: b"t" * 42 + b"\xe9"})
    assert r.status_code == 403


def test_the_token_is_never_accepted_from_the_query_string(client):
    r = client.get(f"/api/bootstrap?token={TOKEN}&{auth.HEADER}={TOKEN}")
    assert r.status_code == 403


def test_the_token_matches_in_constant_time():
    assert auth.matches(TOKEN, TOKEN)
    assert not auth.matches(None, TOKEN)
    assert not auth.matches("", TOKEN)
    assert not auth.matches(TOKEN, "")
    assert not auth.matches("t" * 42 + "é", TOKEN)


# ---- the WebSocket handshake ----


def _ws(client, *, protocols=None, **headers):
    return client.websocket_connect(
        "/ws/conversation/nope", subprotocols=protocols, headers={"host": HOST, **headers},
    )


def test_a_websocket_without_the_token_subprotocol_is_refused(client):
    for protocols in (None, ["qcauth.wrong"], ["chat", "qcauth." + "t" * 42]):
        with pytest.raises(WebSocketDisconnect) as refused, _ws(client, protocols=protocols):
            pass
        assert refused.value.code == 4403 or refused.value.code == 1000


def test_a_websocket_from_a_foreign_origin_is_refused_even_with_the_token(client):
    for origin in ("null", "http://evil.example"):
        with pytest.raises(WebSocketDisconnect), _ws(
            client, protocols=[auth.SUBPROTOCOL_PREFIX + TOKEN], origin=origin,
        ):
            pass


def test_a_websocket_with_the_token_is_let_in_and_echoes_only_that_protocol(client):
    offered = ["chat", auth.SUBPROTOCOL_PREFIX + TOKEN]
    with pytest.raises(WebSocketDisconnect) as closed, _ws(client, protocols=offered) as ws:
        assert ws.accepted_subprotocol == auth.SUBPROTOCOL_PREFIX + TOKEN
        ws.receive_json()
    assert closed.value.code == 4404  # admitted, then told the conversation is unknown


# ---- proving an instance is ours before handing it the token ----


def test_health_proves_it_knows_the_token_without_revealing_it(client):
    challenge = secrets.token_hex(16)
    body = client.get(f"/api/health?challenge={challenge}").json()
    assert body["proof"] == auth.instance_proof(TOKEN, 8642, challenge)
    assert TOKEN not in str(body)
    # Bound to this port: a relay that forwarded the challenge to an instance
    # on another port would carry back a proof the caller rejects.
    assert body["proof"] != auth.instance_proof(TOKEN, 8643, challenge)


@pytest.mark.parametrize("challenge", ["", "short", "x" * 300, "not hex but long enough!!"])
def test_health_answers_no_proof_for_a_malformed_challenge(client, challenge):
    body = client.get("/api/health", params={"challenge": challenge}).json()
    assert body["app"] == "quickcode"
    assert "proof" not in body


# ---- response headers ----


def test_every_response_carries_nosniff_and_refuses_to_be_framed_by_others(client):
    for path in ("/api/health", "/"):
        r = client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_the_shell_is_served_under_a_policy_that_allows_only_its_own_scripts(client):
    if not FRONTEND_DIR.is_dir():
        pytest.skip("frontend not present")
    csp = client.get("/").headers["content-security-policy"]
    directives = dict(
        (part.split()[0], part.split()[1:]) for part in csp.split(";") if part.strip()
    )
    assert directives["script-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert "'unsafe-eval'" not in csp
    # Only this server's own sockets, never a WebSocket to anywhere else.
    assert all(src == "'self'" or src.startswith(("ws://127.0.0.1:8642", "ws://localhost:8642",
                                                   "ws://[::1]:8642"))
               for src in directives["connect-src"])


def test_the_shell_has_no_inline_script_the_policy_would_block():
    """The policy has no ``'unsafe-inline'`` for scripts, so the page must not
    need it: every script is a file, and no element carries an ``on…=``
    handler. A regression here is a blank window, not a warning."""
    if not FRONTEND_DIR.is_dir():
        pytest.skip("frontend not present")
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b[^>]*>", html, flags=re.I):
        assert "src=" in tag, f"inline script in index.html: {tag}"
    assert not re.search(r"\son[a-z]+\s*=", html, flags=re.I)
    for js in FRONTEND_DIR.rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert not re.search(r"\bnew Function\(|\beval\(", text), js
        assert not re.search(r"""<[a-z][^>]*\son[a-z]+=["'$]""", text), js


# ---- every route, enumerated rather than remembered ----


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_http_route_is_under_the_guard_and_refuses_a_missing_token(client):
    """A route module added later is behind the guard because of where it is
    mounted, not because someone remembered: every HTTP route this app serves
    other than the health probe is under ``/api/``, and each one answers a
    token-less request with 403 before its handler runs."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import Mount

    checked = 0
    for route in client.app.routes:
        if isinstance(route, Mount):
            assert route.path == "", f"unexpected mount {route.path}"
            continue
        if isinstance(route, APIWebSocketRoute):
            assert route.path.startswith("/ws/"), route.path
            continue
        assert isinstance(route, APIRoute), f"route outside the guard: {route.path}"
        assert route.path.startswith("/api/"), f"route outside the guard: {route.path}"
        if route.path == "/api/health":
            continue
        for method in route.methods:
            r = client.request(method, _concrete(route.path), json={})
            assert r.status_code == 403, (method, route.path, r.status_code)
            checked += 1
    assert checked > 100


def test_every_websocket_route_refuses_a_handshake_without_the_token(client):
    from fastapi.routing import APIWebSocketRoute

    sockets = [r for r in client.app.routes if isinstance(r, APIWebSocketRoute)]
    assert len(sockets) >= 4
    for route in sockets:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            _concrete(route.path), headers={"host": HOST},
        ):
            pass
