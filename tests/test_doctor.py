import quickcode.doctor as doctor
from quickcode.doctor import (
    Check,
    check_api_key,
    check_config,
    check_git,
    check_pty,
    check_python,
    check_ripgrep,
    check_search,
    format_report,
    main,
    run_checks,
)

VALID_LEVELS = {"ok", "warn", "fail"}

ALL_CHECK_FNS = [
    check_python,
    check_ripgrep,
    check_git,
    check_pty,
    check_api_key,
    check_config,
    check_search,
]


def test_each_check_returns_valid_check():
    for fn in ALL_CHECK_FNS:
        result = fn()
        assert isinstance(result, Check)
        assert result.level in VALID_LEVELS
        assert isinstance(result.name, str) and result.name
        assert isinstance(result.detail, str) and result.detail
        assert isinstance(result.ok, bool)


def test_check_python_is_ok_on_this_interpreter():
    result = check_python()
    assert result.ok is True
    assert result.level == "ok"


def test_check_api_key_fails_when_unset_and_unsaved(monkeypatch):
    from quickcode.secrets import API_KEY_ENV

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setattr("quickcode.secrets.has_saved_key", lambda: False)

    result = check_api_key()
    assert result.ok is False
    assert result.level == "fail"
    assert API_KEY_ENV in result.detail


def test_check_api_key_ok_when_env_set(monkeypatch):
    from quickcode.secrets import API_KEY_ENV

    monkeypatch.setenv(API_KEY_ENV, "sk-test-123")

    result = check_api_key()
    assert result.ok is True
    assert result.level == "ok"


def test_check_api_key_ok_when_saved_key_present(monkeypatch):
    from quickcode.secrets import API_KEY_ENV

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.setattr("quickcode.secrets.has_saved_key", lambda: True)

    result = check_api_key()
    assert result.ok is True
    assert result.level == "ok"


# --------------------------------------------------------------------------
# Web search
# --------------------------------------------------------------------------

SEARCH_ENV_VARS = [
    "QUICKCODE_SEARCH_PROVIDER",
    "QUICKCODE_BRAVE_API_KEY",
    "QUICKCODE_SERPER_API_KEY",
    "QUICKCODE_TAVILY_API_KEY",
    "QUICKCODE_SEARXNG_URL",
    "QUICKCODE_EXA_API_KEY",
    "QUICKCODE_GOOGLE_CSE_API_KEY",
    "QUICKCODE_GOOGLE_CSE_CX",
]


def _isolate_search(monkeypatch, settings=None):
    """No env vars, no stored keys, and a settings block we control."""
    from quickcode.search import SearchSettings

    for var in SEARCH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("quickcode.secrets.load_secret", lambda name: None)
    settings = settings if settings is not None else SearchSettings()
    monkeypatch.setattr(doctor, "_search_settings", lambda: settings)
    return settings


def test_check_search_never_fails(monkeypatch):
    _isolate_search(monkeypatch)
    assert check_search().level != "fail"


def test_check_search_unconfigured_warns_with_signup_and_command(monkeypatch):
    _isolate_search(monkeypatch)

    result = check_search()
    assert result.ok is False
    assert result.level == "warn"
    assert "Brave Search" in result.detail
    assert "https://api-dashboard.search.brave.com/app/keys" in result.detail
    assert "QUICKCODE_BRAVE_API_KEY" in result.detail
    assert "python -m quickcode.search set-key brave" in result.detail


def test_check_search_ok_when_key_in_env(monkeypatch):
    _isolate_search(monkeypatch)
    monkeypatch.setenv("QUICKCODE_BRAVE_API_KEY", "brave-secret-value")

    result = check_search()
    assert result.ok is True
    assert result.level == "ok"
    assert "QUICKCODE_BRAVE_API_KEY" in result.detail
    assert "brave-secret-value" not in result.detail
    assert "secret" not in result.detail


def test_check_search_never_prints_a_key_from_config(monkeypatch):
    from quickcode.search import SearchSettings

    _isolate_search(
        monkeypatch,
        SearchSettings(provider="brave", providers={"brave": {"api_key": "cfg-key-abc"}}),
    )

    result = check_search()
    assert result.level == "ok"
    assert "cfg-key-abc" not in result.detail
    assert "config.json" in result.detail


def test_check_search_searxng_reports_the_instance(monkeypatch):
    from quickcode.search import SearchSettings

    _isolate_search(
        monkeypatch,
        SearchSettings(
            provider="searxng",
            providers={"searxng": {"base_url": "http://localhost:8080"}},
        ),
    )

    result = check_search()
    assert result.level == "ok"
    assert "http://localhost:8080" in result.detail


def test_check_search_google_cse_wants_key_and_cx(monkeypatch):
    from quickcode.search import SearchSettings

    _isolate_search(monkeypatch, SearchSettings(provider="google_cse"))
    monkeypatch.setenv("QUICKCODE_GOOGLE_CSE_API_KEY", "google-key")

    result = check_search()
    assert result.level == "warn"
    assert "QUICKCODE_GOOGLE_CSE_CX" in result.detail
    assert "google-key" not in result.detail


def test_check_search_unknown_provider_warns(monkeypatch):
    from quickcode.search import SearchSettings

    _isolate_search(monkeypatch, SearchSettings(provider="duckduckgo"))

    result = check_search()
    assert result.level == "warn"
    assert "duckduckgo" in result.detail
    assert "brave" in result.detail


def test_check_search_names_a_ready_alternative(monkeypatch):
    from quickcode.search import SearchSettings

    _isolate_search(
        monkeypatch,
        SearchSettings(
            provider="brave",
            providers={"searxng": {"base_url": "https://searx.example.com"}},
        ),
    )

    result = check_search()
    assert result.level == "warn"
    assert "SearXNG" in result.detail
    assert "QUICKCODE_SEARCH_PROVIDER=searxng" in result.detail


def test_check_search_survives_a_broken_search_layer(monkeypatch):
    def boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr(doctor, "_search_settings", boom)

    result = check_search()
    assert result.level == "warn"
    assert result.ok is False


def test_run_checks_includes_search():
    assert any(c.name == "Web search" for c in run_checks())


def test_run_checks_returns_non_empty_list():
    checks = run_checks()
    assert isinstance(checks, list)
    assert len(checks) > 0
    assert all(isinstance(c, Check) for c in checks)


def test_format_report_contains_summary_and_one_line_per_check():
    checks = run_checks()
    report = format_report(checks)

    ok_count = sum(1 for c in checks if c.level == "ok")
    warn_count = sum(1 for c in checks if c.level == "warn")
    fail_count = sum(1 for c in checks if c.level == "fail")
    assert f"{ok_count} ok, {warn_count} warnings, {fail_count} failures" in report

    for c in checks:
        assert c.name in report


def test_main_returns_int():
    result = main()
    assert isinstance(result, int)
    assert result in (0, 1)


def test_module_runnable_as_script():
    assert hasattr(doctor, "main")
