"""Keep credentials out of what a session log writes down.

The log is a plain file in the user's project: gitignored, but attached to bug
reports, synced by backup tools and read by anything that indexes the tree. Two
things keep keys out of it.

**Keys QuickCode holds** -- every key saved from Settings, and every credential
variable in the environment (``secrets.credential_env_names``, the list child
processes are denied) -- are replaced wherever they appear, in any record. Nothing legitimate in a transcript is one
of those strings, so this cannot damage content; it catches the key however it
got there, an echoing proxy or a tool that printed the environment alike.

**Credential shapes in error text** -- ``Bearer …``, an ``Authorization:``
header, ``user:password@`` in a URL, ``?api_key=`` in a query -- are scrubbed
only from text that reports a failure: error events, failed tool results. That
is where HTTP clients and servers put the request they refused. Ordinary
transcript text is left alone, because a file the model read that contains an
example header is the user's content, and replaying it altered would misreport
what the model was shown.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

REDACTED = "[redacted]"

# Shorter values would match ordinary text; no real API key is this short.
_MIN_SECRET = 12
# Keys are printable ASCII with at least one letter. A value with a quote or a
# backslash would not appear verbatim inside a JSON string, and an all-digit one
# could match a JSON number, so those are left to the patterns.
_KEYLIKE = re.compile(r"(?=.*[A-Za-z])[\x21\x23-\x5b\x5d-\x7e]+\Z")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
    (re.compile(
        r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key|x-goog-api-key"
        r"|x-subscription-token)(['\"]?\s*[:=]\s*['\"]?)((?:bearer|basic|token)\s+)?"
        r"(?!\[redacted\])[^\s'\",;}\]]{8,}"),
     r"\1\2\3" + REDACTED),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@'\"]+:)[^/\s@'\"]+@"),
     r"\1" + REDACTED + "@"),
    (re.compile(
        r"(?i)([?&](?:api[_-]?key|apikey|key|access[_-]?token|token|auth|secret)=)"
        r"[^&\s'\"#]{6,}"),
     r"\1" + REDACTED),
]

_cache: dict[str, Any] = {"signature": None, "values": ()}


def known_secrets() -> tuple[str, ...]:
    """Every credential value QuickCode holds right now.

    The environment half is ``secrets.credential_envs_set``: whatever
    ``subproc.child_env`` keeps from a child, the log keeps out as well.
    Cached against those values and the key files' mtimes, so a write costs a
    directory listing rather than a decryption.
    """
    from quickcode import secrets

    env_values = tuple(sorted(secrets.credential_envs_set().values()))
    try:
        files = tuple(sorted(
            (p.name, p.stat().st_mtime_ns) for p in secrets.SECRETS_DIR.glob("*.key")
        ))
    except OSError:
        files = ()
    signature = (env_values, files)
    if signature == _cache["signature"]:
        return _cache["values"]
    found: set[str] = {v for v in env_values if v}
    for name, _mtime in files:
        try:
            value = secrets.load_secret(name[: -len(".key")])
        except Exception:  # noqa: BLE001 - an unreadable key is not a write error
            value = None
        if value:
            found.add(value)
    values = tuple(sorted(
        (v.strip() for v in found
         if len(v.strip()) >= _MIN_SECRET and _KEYLIKE.match(v.strip())),
        key=len, reverse=True,
    ))
    _cache["signature"], _cache["values"] = signature, values
    return values


def scrub_serialized(line: str, secrets: tuple[str, ...]) -> str:
    """``line`` (one serialized record) with every known secret replaced.

    Done on the serialized text because the values are key-like: they appear in
    JSON exactly as they are, so a replace is exact, and one pass over the line
    is far cheaper than walking a record that can carry a whole file.
    """
    for value in secrets:
        if value in line:
            line = line.replace(value, REDACTED)
    return line


def scrub_error_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_event(ev: dict[str, Any]) -> dict[str, Any]:
    kind = ev.get("type")
    if kind == "error" and isinstance(ev.get("message"), str):
        return {**ev, "message": scrub_error_text(ev["message"])}
    if kind == "tool_result" and ev.get("is_error") and isinstance(ev.get("content"), str):
        return {**ev, "content": scrub_error_text(ev["content"])}
    if kind == "agent_event" and isinstance(ev.get("ev"), dict):
        inner = _scrub_event(ev["ev"])
        if inner is not ev["ev"]:
            return {**ev, "ev": inner}
    return ev


def _scrub_message(msg: Any) -> Any:
    if (isinstance(msg, dict) and msg.get("role") == "tool"
            and isinstance(msg.get("content"), str)
            and msg["content"].startswith("[error]")):
        return {**msg, "content": scrub_error_text(msg["content"])}
    return msg


def scrub_error_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """``rec`` with credential shapes removed from the text that reports failures.

    Returns ``rec`` itself when it carries no such text.
    """
    kind = rec.get("kind")
    if kind == "event" and isinstance(rec.get("ev"), dict):
        ev = _scrub_event(rec["ev"])
        return rec if ev is rec["ev"] else {**rec, "ev": ev}
    if kind == "message":
        msg = _scrub_message(rec.get("message"))
        return rec if msg is rec.get("message") else {**rec, "message": msg}
    if kind == "compaction" and isinstance(rec.get("messages"), list):
        return {**rec, "messages": [_scrub_message(m) for m in rec["messages"]]}
    return rec


def scrub_event(ev: dict[str, Any]) -> dict[str, Any]:
    """The event as it may be logged and shown: both scrubs applied.

    Returns ``ev`` itself when nothing in it needed redacting, so the common
    case costs one serialization and no copy.
    """
    cleaned = _scrub_event(ev)
    secrets = known_secrets()
    if secrets:
        text = json.dumps(cleaned, ensure_ascii=False)
        scrubbed = scrub_serialized(text, secrets)
        if scrubbed != text:
            with contextlib.suppress(ValueError):
                cleaned = json.loads(scrubbed)
    return cleaned
