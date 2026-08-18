"""Native application window around the local web UI.

QuickCode serves its frontend on 127.0.0.1, but a browser tab is the wrong
frame for it: address bar, tabs, bookmarks, and a taskbar entry that belongs
to the browser. pywebview wraps the same URL in a real OS window -- on
Windows that is WebView2, which ships with Edge, so there is nothing extra to
install for the user.

When pywebview or its runtime is missing we fall back to the default browser,
which is how every earlier release behaved.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("quickcode.ui.window")

TITLE = "QuickCode"
WIDTH = 1440
HEIGHT = 900
MIN_SIZE = (900, 600)

# The frontend's own icon, read (never edited) from here for the window/
# taskbar icon -- keeps the window in sync with the favicon without a second
# asset to maintain.
_ICON_PATH = Path(__file__).resolve().parent.parent / "frontend" / "assets" / "favicon.ico"

# The WebView2 "Evergreen" runtime's well-known product GUID -- the same one
# Microsoft's own detection samples use. Present on almost every Windows
# 10/11 box already (Edge installs it), but lean/LTSC images and some VMs
# don't have it.
_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_DOCS = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _webview2_runtime_present() -> bool:
    """Best-effort registry probe for the WebView2 Evergreen runtime.

    Without it, pywebview's EdgeChromium backend doesn't fail with a Python
    exception -- initialization happens inside an async .NET callback
    (``EnsureCoreWebView2Async``), so the window just opens blank and stays
    that way, with nothing in the log to explain why. Checking up front turns
    that silent failure into an actionable message and a clean browser
    fallback instead.
    """
    if sys.platform != "win32":
        return True  # the check is Windows/EdgeChromium-specific
    try:
        import webview

        runtime_path = webview.settings.get("WEBVIEW2_RUNTIME_PATH")
    except Exception:
        runtime_path = None
    if runtime_path and os.path.exists(runtime_path):
        return True  # a bundled/fixed-version runtime is configured
    import winreg

    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}",
        ),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
    ]
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
                if value:
                    return True
        except OSError:
            continue
    return False


def available() -> bool:
    """True when a native window can be opened in this environment."""
    try:
        import webview  # noqa: F401
    except Exception:  # not installed, or no GUI toolkit behind it
        return False
    if not _webview2_runtime_present():
        log.warning(
            "WebView2 Runtime not found; falling back to the default browser. "
            "Install it from %s for the native app window.",
            _WEBVIEW2_DOCS,
        )
        return False
    return True


def open_in_browser(url: str) -> None:
    webbrowser.open(url)


def focus_existing() -> bool:
    """Best-effort: bring an already-open QuickCode window to the foreground.

    Used when a second launch hands its project to the already-running
    instance instead of starting a second server (see
    ``webapp.run_webapp``/``_hand_off_to_running_instance``). Windows-only;
    a no-op elsewhere. Windows' focus-stealing prevention means this can
    still just flash the taskbar icon instead of raising the window -- that
    is an OS policy, not a bug here.
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        found: list[int] = []

        def _visit(hwnd: int, _lparam: int) -> bool:
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == TITLE:
                    found.append(hwnd)
                    return False  # stop enumerating
            return True

        user32.EnumWindows(wndenumproc(_visit), 0)
        if not found:
            return False
        hwnd = found[0]
        _SW_RESTORE = 9
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        log.debug("could not focus the existing window", exc_info=True)
        return False


def run(url: str, *, on_close: Callable[[], None]) -> None:
    """Open the window and block until the user closes it.

    Must be called on the main thread -- the GUI toolkits behind pywebview
    all insist on it -- so the server runs in a background thread instead.
    ``on_close`` is what stops that server.
    """
    import webview

    from quickcode.config import CONFIG_DIR, DEFAULT_THEME_COLORS

    # uv-managed venvs on Windows launch python.exe/pythonw.exe through a
    # trampoline that re-execs into the real interpreter as a *child*
    # process (and `quickcode-app`'s gui-script wrapper adds one more such
    # hop on top of that). The window below always belongs to *this*
    # process's PID, which is one or two levels deeper than the PID a
    # launcher script gets back -- log it so that isn't mistaken for the
    # window failing to appear.
    log.debug("creating window %r in pid=%s", TITLE, os.getpid())

    icon = str(_ICON_PATH) if _ICON_PATH.is_file() else None
    webview.create_window(
        TITLE,
        url,
        width=WIDTH,
        height=HEIGHT,
        min_size=MIN_SIZE,
        # Matches the default theme background so the window doesn't flash
        # white while the page (and its saved theme, if different) loads.
        background_color=DEFAULT_THEME_COLORS["background"],
        text_select=True,
    )
    try:
        webview.start(
            # Pin the backend on Windows instead of letting pywebview probe
            # for one -- EdgeChromium is the only backend QuickCode ships
            # for, and probing just adds startup latency. Elsewhere, leave
            # gui=None so pywebview picks whatever GTK/QT backend it finds.
            gui="edgechromium" if sys.platform == "win32" else None,
            # A private (in-memory) profile is pywebview's default, which
            # silently drops the frontend's own localStorage (theme, last
            # model, layout) every run. Pointing WebView2 at a real profile
            # directory under the app's own config dir makes that state
            # persist like any other desktop app's would.
            private_mode=False,
            storage_path=str(CONFIG_DIR / "webview"),
            icon=icon,
        )
    except Exception:
        log.exception(
            "native WebView window failed to start; if this is a fresh "
            "machine, install the WebView2 Runtime from %s",
            _WEBVIEW2_DOCS,
        )
    finally:
        on_close()
