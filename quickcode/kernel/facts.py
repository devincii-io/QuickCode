"""Small derived facts a plugin card shows, computed once on the server.

Each one used to be recomputed by the browser, or not shown at all because the
payload did not carry what it was computed from.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


def tool_signature(name: str, parameters: dict[str, Any] | None) -> str:
    """``read(file_path, offset?, limit?)`` from a tool's declared schema.

    The same reading ``js/config/kinds.js:signatureOf`` makes, so a card shows
    the same line whichever side computed it. Carrying it on the payload is
    what spares the Tools page one schema request per tool.
    """
    params = parameters if isinstance(parameters, dict) else {}
    props = params.get("properties")
    required = set(params.get("required") or ())
    names = list(props) if isinstance(props, dict) else []
    args = [key if key in required else f"{key}?" for key in names]
    return f"{name}({', '.join(args)})"


def display_endpoint(url: str) -> str:
    """A provider base URL as a card may show it: scheme, host, port and path.

    A base URL can carry a credential -- ``https://user:token@host`` or a
    ``?key=`` query -- and the card is the kind of thing that ends up in a
    screenshot. Neither part says which endpoint this is, so both are dropped.
    """
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if not parts.scheme or not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
