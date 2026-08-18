"""Configuring web search from the UI: the bootstrap payload, PUT /api/config's
``search`` block, and POST /api/search-key.

The load-bearing test here is the last one in each group: a key goes in through
the one route that encrypts it, and comes back out of nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.config import Config
from tests.test_server import FakeProvider, make_client, make_manager

BRAVE_KEY = "brave-secret-value-do-not-echo"


def _clear_search_env(monkeypatch):
    """Any of these set on the developer's machine would decide the outcome."""
    from quickcode.search import PROVIDER_CHOICE_ENV, provider_infos

    monkeypatch.delenv(PROVIDER_CHOICE_ENV, raising=False)
    for info in provider_infos():
        extras = (var for _key, var, _label in info.extra_fields)
        for var in (info.api_key_env, info.base_url_env, *extras):
            if var:
                monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect both stores. These tests write a config and a real secret."""
    from quickcode import secrets

    _clear_search_env(monkeypatch)
    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path / "secrets")
    config_path = tmp_path / "config.json"
    real_save = Config.save
    monkeypatch.setattr(Config, "save", lambda self, path=config_path: real_save(self, path))
    return config_path


def _search(client) -> dict:
    return client.get("/api/bootstrap").json()["search"]


def _provider(payload: dict, name: str) -> dict:
    return next(p for p in payload["providers"] if p["name"] == name)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_describes_every_provider(tmp_path, sandbox):
    from quickcode.search import PROVIDERS

    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        payload = _search(client)

    assert payload["provider"] == "brave"  # the documented default
    assert [p["name"] for p in payload["providers"]] == list(PROVIDERS)

    brave = _provider(payload, "brave")
    assert brave["needs_key"] is True
    assert brave["configured"] is False
    assert brave["missing"] == ["an API key"]
    assert brave["signup_url"].startswith("https://")
    assert brave["free_tier"]

    # SearXNG is keyless and wants an instance; Google CSE wants a key and a cx.
    searxng = _provider(payload, "searxng")
    assert searxng["needs_key"] is False
    assert searxng["needs_base_url"] is True
    assert searxng["missing"] == ["the base URL of the instance to query"]

    google = _provider(payload, "google_cse")
    assert google["needs_key"] is True
    assert [f["key"] for f in google["extra_fields"]] == ["cx"]
    assert google["extra_fields"][0]["env"] == "QUICKCODE_GOOGLE_CSE_CX"


def test_bootstrap_reports_where_the_key_comes_from(tmp_path, sandbox, monkeypatch):
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        assert client.post(
            "/api/search-key", json={"provider": "brave", "key": BRAVE_KEY}
        ).status_code == 204
        saved = _provider(_search(client), "brave")

        # An env var outranks the saved key, and the UI has to be able to say so.
        monkeypatch.setenv("QUICKCODE_BRAVE_API_KEY", "from-the-environment")
        env = _provider(_search(client), "brave")

    assert saved["configured"] is True
    assert saved["key_source"] == "the saved (encrypted) key"
    assert saved["key_from_store"] is True

    assert env["key_source"] == "QUICKCODE_BRAVE_API_KEY"
    assert env["key_from_store"] is False


def test_no_get_response_ever_carries_a_key(tmp_path, sandbox):
    """The store is write-only from the browser's side. Nothing reads back."""
    manager = make_manager(tmp_path, FakeProvider([]))
    # A key somebody put in config.json by hand is read by the resolver, so it
    # is the one that could leak through the payload builder. It must not.
    manager.config.search.providers["searxng"] = {"api_key": "hand-written-key-value"}
    with make_client(manager) as client:
        client.post("/api/search-key", json={"provider": "brave", "key": BRAVE_KEY})
        client.put(
            "/api/config",
            json={"search": {"provider": "searxng",
                             "providers": {"searxng": {"base_url": "https://searx.example.org"}}}},
        )
        pid = client.get("/api/projects").json()["projects"][0]["id"]
        bodies = [
            client.get(path).text
            for path in ("/api/bootstrap", f"/api/projects/{pid}/bootstrap", "/api/health")
        ]

    for body in bodies:
        assert BRAVE_KEY not in body
        assert "hand-written-key-value" not in body
        # Not even a fragment: an eight-character prefix is still a key leak.
        assert BRAVE_KEY[:8] not in body
        # ``has_api_key`` and ``api_key_env`` are booleans and names; a field
        # actually called api_key would be the value itself.
        assert '"api_key"' not in body


# ---------------------------------------------------------------------------
# POST /api/search-key
# ---------------------------------------------------------------------------


def test_search_key_is_saved_under_the_provider_name(tmp_path, sandbox):
    from quickcode import secrets
    from quickcode.search import secret_name

    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        assert client.post(
            "/api/search-key", json={"provider": "brave", "key": f"  {BRAVE_KEY}  "}
        ).status_code == 204

    assert secrets.load_secret(secret_name("brave")) == BRAVE_KEY
    assert (tmp_path / "secrets" / "search-brave.key").exists()


def test_search_key_rejects_unknown_and_keyless_providers(tmp_path, sandbox):
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        unknown = client.post("/api/search-key", json={"provider": "ddg", "key": "x"})
        missing = client.post("/api/search-key", json={"key": "x"})
        keyless = client.post("/api/search-key", json={"provider": "searxng", "key": "x"})
        empty = client.post("/api/search-key", json={"provider": "brave", "key": "   "})

    assert unknown.status_code == 400
    assert "unknown search provider" in unknown.json()["detail"]
    assert missing.status_code == 400
    assert keyless.status_code == 400
    assert "no API key" in keyless.json()["detail"]
    assert empty.status_code == 400
    assert not (tmp_path / "secrets").exists()


# ---------------------------------------------------------------------------
# PUT /api/config, search block
# ---------------------------------------------------------------------------


def test_config_saves_provider_and_non_secret_settings(tmp_path, sandbox):
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        assert client.put(
            "/api/config",
            json={
                "search": {
                    "provider": "google_cse",
                    "providers": {"google_cse": {"cx": " engine-id-42 "}},
                }
            },
        ).status_code == 204
        google = _provider(_search(client), "google_cse")

    assert manager.config.search.provider == "google_cse"
    assert manager.config.search.providers["google_cse"]["cx"] == "engine-id-42"
    assert google["extra_fields"][0]["value"] == "engine-id-42"
    # The cx alone is not enough: Google CSE still wants a key.
    assert google["configured"] is False
    assert google["missing"] == ["an API key"]

    written = json.loads(Path(sandbox).read_text(encoding="utf-8"))
    assert written["search"]["provider"] == "google_cse"


def test_config_keeps_a_hand_written_key_it_did_not_put_there(tmp_path, sandbox):
    """The route merges: editing a base URL must not delete somebody's key."""
    manager = make_manager(tmp_path, FakeProvider([]))
    manager.config.search.providers["searxng"] = {"api_key": "hand-written"}
    with make_client(manager) as client:
        assert client.put(
            "/api/config",
            json={"search": {"providers": {"searxng": {"base_url": "https://searx.example.org"}}}},
        ).status_code == 204

    saved = manager.config.search.providers["searxng"]
    assert saved == {"api_key": "hand-written", "base_url": "https://searx.example.org"}


def test_config_refuses_a_key_and_anything_it_does_not_know(tmp_path, sandbox):
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        key = client.put(
            "/api/config", json={"search": {"providers": {"brave": {"api_key": "sk-nope"}}}}
        )
        unknown_setting = client.put(
            "/api/config", json={"search": {"providers": {"brave": {"cx": "nope"}}}}
        )
        unknown_provider = client.put(
            "/api/config", json={"search": {"providers": {"ddg": {"base_url": "x"}}}}
        )
        bad_choice = client.put("/api/config", json={"search": {"provider": "ddg"}})

    assert key.status_code == 400
    assert "/api/search-key" in key.json()["detail"]
    assert unknown_setting.status_code == 400
    assert unknown_provider.status_code == 400
    assert bad_choice.status_code == 400
    assert manager.config.search.providers == {}
    assert manager.config.search.provider == ""
