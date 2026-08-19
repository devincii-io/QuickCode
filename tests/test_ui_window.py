"""Native window helpers: WebView2 detection and single-instance hand-off."""

from __future__ import annotations

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
