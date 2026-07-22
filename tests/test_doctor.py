import quickcode.doctor as doctor
from quickcode.doctor import (
    Check,
    check_api_key,
    check_config,
    check_git,
    check_pty,
    check_python,
    check_ripgrep,
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
