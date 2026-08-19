"""No command may put a console window on the user's screen.

``QuickCodeApp.exe`` is built windowed, so a console program it spawns has no
console to inherit and Windows allocates a fresh one -- visible, on top,
for as long as the command runs. The user's report was a terminal blinking at
them every few seconds.

This is invisible to every other test: the flag changes nothing about a
command's output, its exit code or its timing, only whether a window appears.
So it is asserted structurally, the same way the installer's `/T` is -- a
source-level rule, because the failure it prevents cannot be observed from
inside a test process.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from quickcode import subproc

ROOT = Path(__file__).resolve().parents[1] / "quickcode"

# Where a raw spawn is still correct, and why.
ALLOWED = {
    # The helper itself: this is the one place that calls subprocess directly.
    "subproc.py",
    # The installer has its own window and is meant to be seen. It is also
    # deliberately detached; see `launch_installer`.
    "update.py",
    # Authored command tools declare their own argv and run through the same
    # helper as the bash tool -- checked below rather than by filename.
}

_SPAWN = re.compile(r"\bsubprocess\.(run|Popen|call|check_output|check_call)\b")


def _sources() -> list[Path]:
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_the_helper_asks_windows_for_no_console() -> None:
    assert subproc.NO_WINDOW == (subprocess.CREATE_NO_WINDOW if subproc.IS_WINDOWS else 0)


@pytest.mark.skipif(not subproc.IS_WINDOWS, reason="a console window is a Windows problem")
def test_the_helper_keeps_a_caller_s_own_flags() -> None:
    """It ORs rather than replaces, so a caller can still detach a child."""
    seen: dict = {}

    def fake(argv, **kw):
        seen.update(kw)
        return None

    original = subprocess.Popen
    subprocess.Popen = fake  # type: ignore[assignment]
    try:
        subproc.popen(["x"], creationflags=subprocess.DETACHED_PROCESS)
    finally:
        subprocess.Popen = original  # type: ignore[assignment]

    assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert seen["creationflags"] & subprocess.DETACHED_PROCESS


def test_no_module_spawns_a_console_program_directly() -> None:
    """A bare `subprocess.run` is how the flashing window comes back."""
    offenders: list[str] = []
    for path in _sources():
        if path.name in ALLOWED:
            continue
        for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A comment naming the type it returns is not a spawn. Crude, but
            # the alternative is an AST walk for a rule this simple, and a
            # `#` inside a string literal on a spawn line does not exist.
            line = raw.split("#", 1)[0]
            if _SPAWN.search(line):
                rel = path.relative_to(ROOT.parent)
                offenders.append(f"{rel}:{n}: {raw.strip()}")
    assert not offenders, (
        "these spawn a console program without CREATE_NO_WINDOW; use "
        "`from quickcode import subproc` and call subproc.run/popen:\n  "
        + "\n  ".join(offenders)
    )
