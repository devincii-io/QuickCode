"""Native window helpers: WebView2 detection and single-instance hand-off."""

from __future__ import annotations

import sys

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


def test_running_instance_health_none_when_unreachable():
    # Nothing is listening on this high port during the test run.
    assert webapp._running_instance_health(58643) is None


def test_hand_off_returns_false_when_unreachable(tmp_path):
    assert webapp._hand_off_to_running_instance(58643, tmp_path) is False
