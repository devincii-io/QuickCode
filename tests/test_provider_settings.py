"""Choosing the model provider: the switch itself, where each key lives, and
the Settings routes that drive both.

The load-bearing assertions: an Anthropic key is never stored over the
OpenRouter one nor sent to OpenRouter's URL, and a switch takes effect for the
next conversation without a restart.
"""

from __future__ import annotations

import pytest

from quickcode.config import DEFAULT_BASE_URL, Config, Profile
from quickcode.doctor import check_api_key
from quickcode.providers.anthropic import AnthropicProvider
from quickcode.providers.choice import display_name, switch_provider
from tests.conftest import wait_until
from tests.test_server import FakeProvider, make_client, make_manager

ANTHROPIC_KEY = "sk-ant-secret-do-not-echo"
OPENROUTER_KEY = "sk-or-secret-do-not-echo"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from quickcode import secrets

    for var in (secrets.API_KEY_ENV, *secrets.PROVIDER_KEY_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(secrets, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(secrets, "_SECRET_PATH", tmp_path / "secrets" / "openrouter.key")
    config_path = tmp_path / "config.json"
    real_save = Config.save
    monkeypatch.setattr(Config, "save", lambda self, path=config_path: real_save(self, path))
    # A rebuilt backend warms its model catalog; that must not reach the network.
    listed.clear()
    monkeypatch.setattr(AnthropicProvider, "list_models", _no_network)
    return config_path


listed: list[AnthropicProvider] = []


async def _no_network(self: AnthropicProvider) -> list:
    listed.append(self)
    return []


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def test_switching_to_anthropic_replaces_only_what_was_openrouters_default() -> None:
    cfg = Config()
    cfg.last_model = "openai/gpt-5"
    switch_provider(cfg, "anthropic")

    p = cfg.profile
    assert p.provider == "anthropic"
    assert p.base_url == "https://api.anthropic.com"
    assert (p.orchestrator_model, p.worker_model) == ("claude-opus-5", "claude-sonnet-5")
    assert cfg.last_model == "", "a model the new backend cannot serve is forgotten"
    assert display_name(p) == "Anthropic"


def test_a_switch_keeps_what_the_user_chose() -> None:
    cfg = Config()
    p = cfg.profile
    p.orchestrator_model = "anthropic/claude-opus-4.8-custom"
    p.base_url = "https://gateway.example/v1"
    cfg.last_model = "anthropic/claude-sonnet-4.5"
    switch_provider(cfg, "anthropic")

    assert p.orchestrator_model == "anthropic/claude-opus-4.8-custom"
    assert p.base_url == "https://gateway.example/v1"
    assert cfg.last_model == "anthropic/claude-sonnet-4.5", "the native adapter translates it"


def test_switching_back_restores_the_openrouter_defaults() -> None:
    cfg = Config()
    switch_provider(cfg, "anthropic")
    cfg.last_model = "claude-opus-5"
    switch_provider(cfg, "openai-compat")

    p, fresh = cfg.profile, Profile()
    assert (p.base_url, p.orchestrator_model, p.worker_model) == (
        DEFAULT_BASE_URL, fresh.orchestrator_model, fresh.worker_model)
    assert cfg.last_model == ""


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_each_provider_keeps_its_own_key(sandbox, monkeypatch) -> None:
    from quickcode import secrets

    secrets.save_provider_key("openai-compat", OPENROUTER_KEY)
    secrets.save_provider_key("anthropic", ANTHROPIC_KEY)

    assert Profile(provider="openai-compat").api_key == OPENROUTER_KEY
    assert Profile(provider="anthropic").api_key == ANTHROPIC_KEY
    assert Profile(provider="anthropic").api_key_env == "QUICKCODE_ANTHROPIC_API_KEY"

    monkeypatch.setenv("QUICKCODE_ANTHROPIC_API_KEY", "from-env")
    assert Profile(provider="anthropic").api_key == "from-env"
    assert Profile(provider="openai-compat").api_key == OPENROUTER_KEY


def test_doctor_checks_the_key_of_the_provider_in_use(sandbox) -> None:
    from quickcode import secrets

    secrets.save_provider_key("openai-compat", OPENROUTER_KEY)
    missing = check_api_key("anthropic")
    assert missing.ok is False and "QUICKCODE_ANTHROPIC_API_KEY" in missing.detail

    secrets.save_provider_key("anthropic", ANTHROPIC_KEY)
    assert check_api_key("anthropic").ok is True


# ---------------------------------------------------------------------------
# Settings routes
# ---------------------------------------------------------------------------


def test_settings_lists_the_providers_and_never_a_key(tmp_path, sandbox) -> None:
    from quickcode import secrets

    secrets.save_provider_key("anthropic", ANTHROPIC_KEY)
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        resp = client.get("/api/bootstrap")

    bs = resp.json()
    assert bs["model_provider"] == "openai-compat"
    by_name = {p["name"]: p for p in bs["model_providers"]}
    assert by_name["anthropic"]["has_api_key"] is True
    assert by_name["anthropic"]["base_url"] == "https://api.anthropic.com"
    assert by_name["anthropic"]["api_key_env"] == "QUICKCODE_ANTHROPIC_API_KEY"
    assert by_name["openai-compat"]["active"] is True
    assert ANTHROPIC_KEY not in resp.text


def test_choosing_anthropic_takes_effect_for_the_next_conversation(tmp_path, sandbox) -> None:
    from quickcode import secrets

    secrets.save_provider_key("anthropic", ANTHROPIC_KEY)
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        assert client.put("/api/config", json={"provider": "anthropic"}).status_code == 204
        bs = client.get("/api/bootstrap").json()
        assert wait_until(lambda: bool(listed))

    assert isinstance(manager.provider, AnthropicProvider)
    assert manager.provider.base_url == "https://api.anthropic.com"
    assert manager.provider._api_key == ANTHROPIC_KEY
    assert listed == [manager.provider], "the catalog comes from the new backend"
    assert bs["model_provider"] == "anthropic"
    assert bs["provider"] == "Anthropic"
    assert bs["api_key_env"] == "QUICKCODE_ANTHROPIC_API_KEY"
    saved = Config.load(sandbox)
    assert saved.profile.provider == "anthropic"
    assert saved.profile.orchestrator_model == "claude-opus-5"


def test_an_unknown_provider_is_refused_and_nothing_changes(tmp_path, sandbox) -> None:
    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        resp = client.put("/api/config", json={"provider": "nonesuch"})

    assert resp.status_code == 400
    assert manager.provider is provider
    assert manager.config.profile.provider == "openai-compat"


def test_a_key_is_stored_against_the_provider_it_belongs_to(tmp_path, sandbox) -> None:
    from quickcode import secrets

    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        assert client.post(
            "/api/apikey", json={"key": ANTHROPIC_KEY, "provider": "anthropic"}
        ).status_code == 204
        # Not the active backend: saving its key leaves the running one alone.
        assert manager.provider is provider
        assert client.post("/api/apikey", json={"key": "x", "provider": "nope"}).status_code == 400

    assert secrets.load_provider_key("anthropic") == ANTHROPIC_KEY
    assert secrets.load_provider_key("openai-compat") is None, "the OpenRouter key is untouched"


def test_a_new_key_for_the_active_provider_is_used_without_a_restart(tmp_path, sandbox) -> None:
    manager = make_manager(tmp_path, FakeProvider([]))
    manager.config.profile.provider = "anthropic"
    with make_client(manager) as client:
        assert client.post("/api/apikey", json={"key": ANTHROPIC_KEY}).status_code == 204

    assert isinstance(manager.provider, AnthropicProvider)
    assert manager.provider._api_key == ANTHROPIC_KEY


def test_a_refused_save_does_not_switch_the_backend_halfway(tmp_path, sandbox) -> None:
    """Switched in memory but never saved or rebuilt, the config and the
    running backend would disagree -- and the next save would not notice."""
    provider = FakeProvider([])
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        resp = client.put("/api/config", json={"provider": "anthropic",
                                               "search": {"provider": "nonesuch"}})

    assert resp.status_code == 400
    assert manager.config.profile.provider == "openai-compat"
    assert manager.provider is provider
