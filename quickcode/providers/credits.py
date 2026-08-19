"""What is left to spend, when the provider will say.

Written after a run stopped on ``402 Insufficient credits``: the balance was the
one number that decided whether the next request would work at all, and the app
had no idea what it was. OpenRouter publishes it; most OpenAI-compatible
endpoints do not, and this says so rather than guessing.

Deliberately not part of the ``Provider`` protocol. A balance is a property of
an *account at a vendor*, not of the chat interface a provider implements, and
folding it into the protocol would oblige every future adapter to answer a
question most of them cannot.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("quickcode.credits")

# Vendors whose credit endpoint we know. The key is a hostname fragment, so a
# self-hosted proxy in front of OpenRouter still matches.
OPENROUTER_HOST = "openrouter.ai"

TIMEOUT_S = 6.0


def supported(base_url: str) -> bool:
    """Whether the balance can be asked for at all at this endpoint."""
    return OPENROUTER_HOST in (base_url or "")


async def fetch(base_url: str, api_key: str, *, transport: Any = None) -> dict[str, Any]:
    """The account's balance, or a plain reason why there is none to show.

    Never raises: this is decoration on a status bar, and a provider being slow
    or down must not turn into a failed request in the UI. The shape is always
    the same, so the caller has one branch rather than four.
    """
    out: dict[str, Any] = {
        "supported": supported(base_url),
        "available": None,
        "used": None,
        "total": None,
        "currency": "USD",
        "error": "",
    }
    if not out["supported"]:
        out["error"] = "this provider does not publish a balance"
        return out
    if not api_key:
        out["error"] = "no API key set"
        return out

    import httpx

    url = base_url.rstrip("/") + "/credits"
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_S) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code != 200:
                out["error"] = f"provider answered {resp.status_code}"
                return out
            body = resp.json()
    except Exception as exc:  # noqa: BLE001 - network, JSON, anything
        log.debug("credits lookup failed: %s", exc)
        out["error"] = "could not reach the provider"
        return out

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        out["error"] = "unexpected answer from the provider"
        return out

    total = _number(data.get("total_credits"))
    used = _number(data.get("total_usage"))
    out["total"] = total
    out["used"] = used
    if total is not None and used is not None:
        # What matters is what is left; the two halves are kept so the UI can
        # say "of" without a second request.
        out["available"] = round(total - used, 6)
    else:
        out["error"] = "the provider did not report a balance"
    return out


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
