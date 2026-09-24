"""Response headers that hold the frontend to what it actually does.

The shell is one module script and a set of stylesheets, all served from this
origin, talking to this origin over fetch and WebSocket. The policy says
exactly that, so text a model or a tool result manages to get rendered as
markup still cannot run script, load anything from elsewhere, or send
anything anywhere -- and no other site can frame the window.

Styles keep ``'unsafe-inline'``: the transcript, trajectory and terminal set
``style=`` attributes in the markup they build, and a style cannot run code.
Scripts do not: nothing in the frontend is inline, and
``tests/test_loopback_security.py`` keeps it that way.
"""

from __future__ import annotations

from collections.abc import Iterable


def content_security_policy(hosts: Iterable[str]) -> str:
    # A host-source cannot spell an IPv6 literal, and a browser drops the
    # whole entry with a console warning; ``'self'`` still covers a page
    # that was loaded from [::1].
    sockets = " ".join(f"ws://{h}" for h in sorted(hosts) if not h.startswith("["))
    return "; ".join((
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        f"connect-src 'self' {sockets}",
        "frame-src 'self'",
        "frame-ancestors 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
    ))


def security_headers(hosts: Iterable[str]) -> dict[str, str]:
    return {
        "Content-Security-Policy": content_security_policy(hosts),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
