"""``quickcode doctor`` diagnostics.

Self-contained health checks for the current environment: interpreter
version, external tool availability (ripgrep, git), PTY backend
importability, API key resolution, user config loadability, and whether
``web_search`` has a provider it can actually reach.

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


def _search_settings():
    """The ``search`` block of the user's config, or None if it cannot be read."""
    try:
        from quickcode.config import Config

        return Config.load().search
    except Exception:  # noqa: BLE001 - a broken config is check_config's problem
        return None


def _search_key_source(info, settings) -> str:
    """Where the key came from — never the key itself, not even truncated."""
    import os

    configured = settings.for_provider(info.name) if settings else {}
    if configured.get("api_key"):
        return "config.json"
    if os.environ.get(info.api_key_env):
        return info.api_key_env
    return "the saved (encrypted) key"


def _search_ready_detail(info, credentials, settings) -> str:
    bits = []
    if info.needs_key:
        bits.append(f"API key resolved from {_search_key_source(info, settings)}")
    if info.needs_base_url and credentials.base_url:
        bits.append(f"instance {credentials.base_url}")
    for key, _var, _label in info.extra_fields:
        if credentials.extra.get(key):
            bits.append(f"{key} set")
    return ", ".join(bits) or "configured"


def check_search() -> Check:
    """Web search is optional: an unconfigured provider warns, it never fails.

    QuickCode works without search exactly as it works without ripgrep, so the
    worst this returns is a warning that names the signup page, the environment
    variable and the ``set-key`` command for the provider actually selected —
    never for a provider the user did not pick, and never any part of a key.
    """
    name = "Web search"
    try:
        from quickcode.search import (
            PROVIDER_CHOICE_ENV,
            PROVIDERS,
            chosen_provider,
            configured_providers,
            provider_names,
            resolve_credentials,
        )

        settings = _search_settings()
        chosen = chosen_provider(settings)
        if chosen not in PROVIDERS:
            return Check(
                name,
                False,
                "warn",
                f"unknown provider {chosen!r} selected — known: "
                f"{', '.join(provider_names())} (set search.provider in "
                f"~/.quickcode/config.json, or {PROVIDER_CHOICE_ENV})",
            )

        info = PROVIDERS[chosen].info
        credentials, missing = resolve_credentials(info, settings)
        if not missing:
            return Check(
                name,
                True,
                "ok",
                f"{info.label} — {_search_ready_detail(info, credentials, settings)}",
            )

        detail = (
            f"{info.label} selected but not configured (missing "
            f"{', and '.join(missing)}) — web_search will fail until it is; "
            f"nothing else is affected. Get it at {info.signup_url}"
        )
        if info.free_tier:
            detail += f" (free tier: {info.free_tier})"
        if info.needs_key and not credentials.api_key:
            detail += (
                f", then set {info.api_key_env} or run: "
                f"python -m quickcode.search set-key {info.name}"
            )
        if info.needs_base_url and not credentials.base_url:
            detail += f", then set {info.base_url_env} to the instance URL"
        for key, var, label in info.extra_fields:
            if not credentials.extra.get(key):
                detail += f"; set {var} to {label}"
        others = [n for n in configured_providers(settings) if n != info.name]
        if others:
            labels = ", ".join(PROVIDERS[n].info.label for n in others)
            detail += (
                f". Configured and ready instead: {labels} — QuickCode will not "
                f"switch on its own ({PROVIDER_CHOICE_ENV}={others[0]} if that is "
                "what you want)"
            )
        return Check(name, False, "warn", detail)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never be the crash
        return Check(name, False, "warn", f"could not inspect search config: {exc}")


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
        check_search(),
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
