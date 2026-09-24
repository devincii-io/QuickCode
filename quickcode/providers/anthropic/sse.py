"""Server-sent events, just enough of the spec for the Messages API stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any


class MalformedEvent(ValueError):
    """An event whose ``data`` was not a JSON object."""


async def events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield each event's JSON payload, in order.

    The payload carries its own ``type``, which is what the API documents as
    authoritative, so the ``event:`` line is only a fallback for a payload
    that omits it.
    """
    name = ""
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield _decode(name, data)
            name, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
    if data:
        yield _decode(name, data)


def _decode(name: str, data: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads("\n".join(data))
    except ValueError as exc:
        raise MalformedEvent(f"unreadable {name or 'stream'} event from the API") from exc
    if not isinstance(payload, dict):
        raise MalformedEvent(f"unreadable {name or 'stream'} event from the API")
    payload.setdefault("type", name)
    return payload
