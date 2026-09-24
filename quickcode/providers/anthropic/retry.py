"""When to send a failed request again, and how long to wait first."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

# 408 and 409 are the API's "try again" conflicts; 429 is a rate limit; 529 is
# "overloaded"; any other 5xx is the service's own failure.
RETRY_STATUSES = frozenset({408, 409, 429})


def retryable_status(status: int) -> bool:
    return status in RETRY_STATUSES or status >= 500


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    # A server may ask for a long pause (a spend-cap 429 can). Waiting that out
    # inside one request would look like a hang, so the wait is capped and the
    # retry budget ends the attempt instead.
    max_retry_after_s: float = 60.0

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        asked = parse_retry_after(retry_after)
        if asked is not None:
            return min(asked, self.max_retry_after_s)
        backoff = min(self.base_delay_s * (2**attempt), self.max_delay_s)
        return backoff + random.uniform(0, backoff / 4)


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds: either a number or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def describe(response: httpx.Response, body: bytes) -> str:
    """A one-line account of a failed response: status, the API's own error
    type and message, and the request id support will ask for. Never headers
    beyond that id -- the request carried the API key."""
    kind, message = "", ""
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        kind = str(payload["error"].get("type") or "")
        message = str(payload["error"].get("message") or "")
    elif body:
        message = body[:300].decode("utf-8", "replace").strip()
    text = f"Anthropic API {response.status_code}" + (f" {kind}" if kind else "")
    if message:
        text += f": {message}"
    request_id = response.headers.get("request-id")
    if request_id:
        text += f" (request {request_id})"
    return text
