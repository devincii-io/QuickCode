"""What an MCP server says, turned into what a model provider will accept.

Two translations, both pure:

**Tool names.** A server names its tools however it likes (``files.read``,
``search docs``, a 90-character name), and the config names the server however
the user likes. Providers do not: a function name must match
``^[A-Za-z0-9_-]{1,64}$``, and one tool outside that shape fails *every* request
the session makes, not just calls to that tool. A name that already fits is
used as written, so rules like ``mcp__docs__search`` keep matching; one that
does not is cleaned and given a short hash of the original, which keeps it
stable across restarts (rules and session logs refer to it) and keeps two
different originals that clean to the same text apart.

**Results.** ``content`` is a list of typed blocks. Text is text; an embedded
resource usually carries text too and is shown with its URI; a resource link is
named; binary blocks are described rather than pasted as base64 into the
model's context. A result with no content but ``structuredContent`` is shown as
that JSON, which is what the spec says an unstructured client should see.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_WIRE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_MAX = 64
_DIGEST = 8


def server_prefix(server: str) -> str:
    """``mcp__<server>__`` as tool names from this server begin, when they fit."""
    return f"mcp__{_UNSAFE.sub('_', server)}__"


def tool_name(server: str, tool: str) -> str:
    raw = f"mcp__{server}__{tool}"
    if _WIRE_NAME.match(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST]
    clean = _UNSAFE.sub("_", raw)[: _MAX - _DIGEST - 1]
    return f"{clean}_{digest}"


def input_schema(raw: Any) -> dict[str, Any]:
    """The tool's schema as a provider will take it: always an object schema."""
    if not isinstance(raw, dict) or not raw:
        return {"type": "object", "properties": {}}
    if "type" not in raw:
        return {"type": "object", **raw}
    return raw


def render_result(result: Any) -> tuple[str, bool]:
    """``(text for the model, is_error)`` for one ``tools/call`` result."""
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False), False
    parts = [_block(item) for item in result.get("content") or [] if isinstance(item, dict)]
    parts = [p for p in parts if p]
    if not parts and "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    return "\n".join(parts), bool(result.get("isError"))


def _block(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "text":
        return str(item.get("text", ""))
    if kind == "resource":
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        uri = resource.get("uri", "")
        if isinstance(resource.get("text"), str):
            return f"[resource {uri}]\n{resource['text']}"
        return f"[resource {uri} ({resource.get('mimeType', 'binary')}) not shown]"
    if kind == "resource_link":
        label = item.get("name") or item.get("title") or ""
        return f"[resource link: {label} {item.get('uri', '')}]".replace("  ", " ")
    if kind in ("image", "audio"):
        size = len(str(item.get("data", ""))) * 3 // 4
        return f"[{kind} {item.get('mimeType', '')}, {size} bytes, not shown]"
    return f"[{kind} content not shown]"
