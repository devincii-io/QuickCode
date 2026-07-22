"""``quickcode doctor`` diagnostics.

Self-contained health checks for the current environment: interpreter
version, external tool availability (ripgrep, git), PTY backend
importability, API key resolution, and user config loadability.

Each check is a small pure function returning a :class:`Check` — no
printing, no side effects — so they're easy to unit test individually.
:func:`run_checks` runs them all in a sensible order and :func:`format_report`
renders the results as a plain-text checklist. :func:`main` is the CLI entry
point; this module is intentionally NOT wired into ``quickcode/cli.py`` here
(that wiring is left to a follow-up change) but can be invoked directly via
``python -m quickcode.doctor``.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

Level = str  # "ok" | "warn" | "fail"

_GLYPHS = {"ok": "✓", "warn": "⚠", "fail": "✗"}


@dataclass
class Check:
    name: str
    ok: bool
    level: Level
    detail: str


def check_python() -> Check:
    """Python >= 3.12 is required; fail below that."""
    info = sys.version_info
    version_str = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= (3, 12):
        return Check("Python version", True, "ok", f"{version_str} (>= 3.12)")
    return Check(
        "Python version", False, "fail", f"{version_str} — QuickCode requires Python >= 3.12"
    )


def check_ripgrep() -> Check:
    """ripgrep (rg) is optional: QuickCode has a pure-Python fallback."""
    path = shutil.which("rg")
    if path:
        return Check("ripgrep (rg)", True, "ok", f"found at {path}")
    return Check(
        "ripgrep (rg)",
        False,
        "warn",
        "not found on PATH — QuickCode falls back to a pure-Python search, "
        "but installing ripgrep is faster",
    )


def check_git() -> Check:
    """git is optional but recommended for repo-aware features."""
    path = shutil.which("git")
    if path:
        return Check("git", True, "ok", f"found at {path}")
    return Check(
        "git", False, "warn", "not found on PATH — git-aware features (branch detection, etc.) are disabled"
    )


def check_pty() -> Check:
    """PTY backend: winpty (ConPTY) on Windows, stdlib pty on POSIX."""
    if not sys.platform.startswith("win"):
        return Check("PTY backend", True, "ok", "n/a on this platform (uses stdlib pty)")
    try:
        import winpty  # noqa: F401
    except ImportError:
        return Check(
            "PTY backend",
            False,
            "warn",
            "winpty not importable — PTY features degrade to a plain subprocess "
            "(no color/tty passthrough)",
        )
    return Check("PTY backend", True, "ok", "winpty importable (ConPTY available)")


def check_api_key() -> Check:
    """API key: env var first, then a saved (DPAPI-encrypted) key."""
    import os

    from quickcode.secrets import API_KEY_ENV, has_saved_key

    if os.environ.get(API_KEY_ENV):
        return Check("API key", True, "ok", f"resolved from {API_KEY_ENV}")
    if has_saved_key():
        return Check("API key", True, "ok", "resolved from saved (encrypted) key")
    return Check(
        "API key",
        False,
        "fail",
        f"not set — set {API_KEY_ENV} or save a key in Settings",
    )


def check_config() -> Check:
    """User config (~/.quickcode/config.json) loads without error."""
    from quickcode.config import Config

    try:
        Config.load()
    except Exception as exc:  # noqa: BLE001 - surface any load failure as a warning
        return Check("Config", False, "warn", f"failed to load: {exc}")
    return Check("Config", True, "ok", "loaded")


def run_checks() -> list[Check]:
    """Run all checks in a sensible order."""
    return [
        check_python(),
        check_git(),
        check_ripgrep(),
        check_pty(),
        check_config(),
        check_api_key(),
    ]


def format_report(checks: list[Check]) -> str:
    """Render a plain-text, markup-free checklist with a trailing summary."""
    name_width = max((len(c.name) for c in checks), default=0)
    lines = []
    for c in checks:
        glyph = _GLYPHS.get(c.level, "?")
        lines.append(f"{glyph} {c.name.ljust(name_width)}  {c.detail}")

    ok_count = sum(1 for c in checks if c.level == "ok")
    warn_count = sum(1 for c in checks if c.level == "warn")
    fail_count = sum(1 for c in checks if c.level == "fail")
    lines.append(f"\n{ok_count} ok, {warn_count} warnings, {fail_count} failures")

    return "\n".join(lines)


def main() -> int:
    checks = run_checks()
    report = format_report(checks)
    # Some Windows consoles (and piped/non-tty output) default to a legacy
    # codepage (e.g. cp1252) that can't encode the glyphs below; fall back to
    # replacing unencodable characters rather than crashing the diagnostic.
    try:
        print(report)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.buffer.write(report.encode(encoding, errors="replace") + b"\n")
    return 1 if any(c.level == "fail" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
