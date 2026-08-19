# PyInstaller recipe for the installed Windows app folder.
#
# Two executables out of one folder, because QuickCode is two things:
#
#   quickcode.exe     console (console=True) — the CLI a pip install would put
#                     on PATH: ``quickcode -p "..."``, ``quickcode doctor``,
#                     ``--version``, ``qc .`` (``main``).
#   QuickCodeApp.exe  windowed (console=False) — what the Start Menu, the
#                     desktop shortcut and the Explorer context menu run. Opens
#                     the app window on the user's home directory (``main_app``).
#
# The windowed one is NOT called QuickCode.exe, and cannot be: Windows file
# names are case-insensitive, so ``QuickCode.exe`` and ``quickcode.exe`` are one
# file in one directory. PyInstaller builds both happily and then silently
# overwrites the first with the second — one 10 MB exe in dist/, named for the
# window and containing the CLI. The console name is the one that had to
# survive verbatim, because it is what a user types.
#
# They share one COLLECT, so the ~40 MB of Python runtime, FastAPI, the OpenAI
# SDK and the frontend are on disk once. PyInstaller still needs one Analysis
# per entry script; the two shims live in packaging/ (see entry_app.py).

import os

import winpty
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# ---------------------------------------------------------------- data files
#
# The frontend is served by StaticFiles and resolved relative to the *module*
# path (``server/app.py``: ``Path(__file__).parent.parent / "frontend"``), and
# the window icon the same way (``ui/window.py``). Frozen, ``__file__`` for a
# packaged module is ``<_internal>/quickcode/server/app.py``, so laying the
# tree down at the matching relative path makes both lookups resolve with no
# ``sys._MEIPASS`` branch in the application code at all.
datas = [("quickcode/frontend", "quickcode/frontend")]

# ``importlib.metadata.version("quickcode")`` is read by ``--version``, by
# ``/api/health`` and the bootstrap payload, and by the update check — which
# compares it against the newest GitHub release. Without the dist-info the
# frozen build answers ``0.0.0-dev`` and would offer an update forever.
datas += copy_metadata("quickcode")

# --------------------------------------------------------------- imports
#
# uvicorn's protocol implementations are chosen by *string* at runtime, and
# ``quickcode/webapp.py`` pins the three below in its ``uvicorn.Config(...)``
# so the frozen build cannot resolve one that was never bundled. These are the
# same three ``auto`` picks today, given what is installed: h11 (no httptools),
# websockets-sansio (websockets present), plain asyncio (no uvloop).
hiddenimports = [
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_sansio_impl",
]

# pywebview picks its backend at ``webview.start()`` time from a string;
# nothing imports ``webview.platforms.edgechromium`` statically. The bundled
# WebView2 interop DLLs come from PyInstaller's own webview hook.
hiddenimports += collect_submodules("webview")

# The OpenAI SDK is imported inside ``OpenAICompatProvider.client`` on purpose
# (~800 ms of import time that a ``--version`` must not pay), and it resolves
# its own resource modules lazily through a module-level ``__getattr__``. Both
# halves are invisible to a static graph, so take the whole package.
hiddenimports += collect_submodules("openai")

# QuickCode's own dynamically-reached corners. Nothing here uses
# ``importlib.import_module`` (unlike QuickTerm's server handlers) — every
# server handler imports its module with a function-level ``from ... import``,
# which the graph does follow. These are the ones a graph *cannot* follow:
hiddenimports += [
    # Entry-point plugins (quickcode/plugins/loader.py) are discovered through
    # importlib.metadata, so the built-in provider factory it falls back to is
    # only ever named as a string in the factory table.
    "quickcode.providers.openai_compat",
    # Reached only via winpty below, and only on the ConPTY path.
    "quickcode.pty.session",
    "winpty",
]

# ------------------------------------------------------------- pywinpty
#
# pywinpty spawns two helper executables at runtime to host the pseudoconsole
# (OpenConsole.exe for the ConPTY backend, winpty-agent.exe for the legacy
# one). PyInstaller follows the DLL imports of _winpty.pyd but never sees these
# spawned EXEs, so without this the bash tool's PTY path dies instantly with
# 0xC000013A ("the console was closed") and silently degrades to a plain
# subprocess — no colors, no tty code paths. They must sit next to
# conpty.dll/winpty.dll inside the winpty/ package directory.
_winpty_dir = os.path.dirname(winpty.__file__)
_winpty_names = ("OpenConsole.exe", "winpty-agent.exe", "conpty.dll", "winpty.dll")
_missing_winpty = [
    name for name in _winpty_names if not os.path.exists(os.path.join(_winpty_dir, name))
]
if _missing_winpty:
    raise RuntimeError(
        f"release build missing required pywinpty helpers: {_missing_winpty} — "
        "install the pty extra (`uv sync`)"
    )
binaries = [(os.path.join(_winpty_dir, name), "winpty") for name in _winpty_names]

# ------------------------------------------------------------------ excludes
#
# Alternative implementations of things already pinned above, plus the test and
# lint toolchain that a dev environment drags in. None of them is reachable
# from either entry point; each one bundled is dead weight in every install.
# (There is no POSIX-only PTY module to drop: quickcode/pty/session.py branches
# on sys.platform inside one file and the POSIX half is stdlib.)
excludes = [
    "httptools",
    "watchfiles",
    "uvloop",
    "wsproto",
    "uvicorn.loops.uvloop",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    # Deprecated in uvicorn 0.52 and not what ``auto`` picks any more; webapp.py
    # asks for the sansio implementation by name.
    "uvicorn.protocols.websockets.websockets_impl",
    "pytest",
    "_pytest",
    "ruff",
    "tests",
    # PyInstaller's own build-time machinery, reachable through the hooks.
    "PyInstaller",
    "setuptools",
    "pip",
    "tkinter",
]


# Analysis, PYZ, EXE, COLLECT and SPECPATH are injected into this file's
# namespace by PyInstaller; a .spec is executed, not imported.
def analyse(script):
    """One Analysis per executable, off the same inputs.

    The two graphs come out near-identical (both entry points import
    ``quickcode.cli``), which is what makes a single shared COLLECT correct.
    """
    return Analysis(
        [os.path.join(SPECPATH, "packaging", script)],
        pathex=[SPECPATH],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
        optimize=1,
    )


app_analysis = analyse("entry_app.py")
cli_analysis = analyse("entry_cli.py")

ICON = os.path.join(SPECPATH, "packaging", "quickcode.ico")

app_exe = EXE(
    PYZ(app_analysis.pure),
    app_analysis.scripts,
    [],
    exclude_binaries=True,
    name="QuickCodeApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX mangles the pseudoconsole helpers (OpenConsole.exe/conpty.dll) and the
    # WebView2 loader, which breaks the bash tool's PTY path and the app window.
    upx=False,
    # Windowed: no console flashes up behind the app window. sys.stdout and
    # sys.stderr are None here, which is exactly the pythonw shape
    # ``main_app`` already patches up via ``_bind_null_streams``.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

cli_exe = EXE(
    PYZ(cli_analysis.pure),
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="quickcode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

# Install as a real application folder rather than a self-extracting one-file
# executable. A one-file build expands the whole ~40 MB runtime into a private
# %TEMP%\_MEIxxxx tree on *every* launch; QuickTerm measured 2.73 s → 1.60 s
# to a bound port moving off it, and QuickCode pays that cost twice over
# because ``quickcode.exe`` and ``QuickCode.exe`` would each carry their own.
if os.path.basename(app_exe.name).lower() == os.path.basename(cli_exe.name).lower():
    # See the header. This failure mode is silent — the build succeeds and one
    # of the two executables is simply not there — so it is checked here.
    raise RuntimeError(
        f"the two executables collide on a case-insensitive filesystem: "
        f"{app_exe.name} / {cli_exe.name}"
    )

coll = COLLECT(
    app_exe,
    cli_exe,
    app_analysis.binaries,
    app_analysis.datas,
    strip=False,
    upx=False,
    name="QuickCode",
)
