"""Full-text search over one project's session logs.

What is searched: each session's title, what the user typed
(``user_message``), what the agent answered (``assistant_message``) and the
names of the tools it called (``tool_call``). A log written before the event
log existed has no such events and is searched through its ``message`` records
instead, the same fallback ``SessionStore.replay_events`` makes.

The query is plain text: case-insensitive terms separated by whitespace, all of
which must occur in the same title or message (AND), with a double-quoted run
counting as one term. There are deliberately no regular expressions -- the
query arrives over HTTP, and ``re`` offers no way to stop a pattern that
backtracks for ever.

A search is bounded by the number of sessions it answers with, the bytes of log
it reads and the time it takes. Logs are read newest first, so whatever a limit
cuts off is the oldest history, and the answer says which limit stopped it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from quickcode.session.records import parse
from quickcode.session.store import SessionStore, safe_conv_id, strip_reminders

MAX_QUERY = 200
MAX_TERMS = 8
SNIPPET = 180
# A line longer than this is not a message anyone typed or read; it is skipped
# rather than held in memory whole.
LINE_CAP = 4 << 20
_CHECK_EVERY = 256

_TERM = re.compile(r'"([^"]+)"|([^\s"]+)')
# Byte tokens only an event record of the transcript kinds carries: inside a
# JSON string a quote is escaped, so a bare `"user_message"` is structure.
_TRANSCRIPT_TOKENS = (b'"user_message"', b'"assistant_message"', b'"tool_call"',
                      b'"tool_result"')


class QueryError(ValueError):
    """The query cannot be searched for; the message says why."""


@dataclass(frozen=True)
class Limits:
    max_sessions: int = 30
    max_hits: int = 3
    max_bytes: int = 256 << 20
    seconds: float = 1.5


@dataclass
class _Budget:
    limits: Limits
    clock: Callable[[], float]
    started: float
    spent: int = 0
    stopped: str | None = None

    def take(self, n: int) -> bool:
        if self.spent + n > self.limits.max_bytes:
            self.stopped = "bytes"
            return False
        self.spent += n
        return True

    def in_time(self) -> bool:
        if self.clock() - self.started > self.limits.seconds:
            self.stopped = "time"
            return False
        return True


@dataclass
class _Found:
    hits: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0

    def add(self, hit: dict[str, Any], keep: int) -> None:
        self.count += 1
        if len(self.hits) < keep:
            self.hits.append(hit)


def parse_query(query: str) -> list[str]:
    """The lower-cased terms of ``query``; ``QueryError`` if it has none."""
    text = str(query or "").strip()
    if len(text) > MAX_QUERY:
        raise QueryError(f"a search is at most {MAX_QUERY} characters")
    terms: list[str] = []
    for m in _TERM.finditer(text):
        term = " ".join((m.group(1) or m.group(2)).split()).lower()
        if term and term not in terms:
            terms.append(term)
    if sum(len(t) for t in terms) < 2:
        raise QueryError("search for at least two characters")
    if len(terms) > MAX_TERMS:
        raise QueryError(f"a search has at most {MAX_TERMS} terms")
    return terms


def _needles(terms: list[str]) -> list[bytes] | None:
    """Byte strings every matching line must contain, when that is provable.

    The log is UTF-8 JSON written with ``ensure_ascii=False``, so a printable
    ASCII term without a quote or backslash appears in the raw line exactly as
    it does in the decoded text, and a line that lacks one after ASCII
    lower-casing cannot match. Anything else is decoded and checked in full.
    """
    if all(t.isascii() and t.isprintable() and '"' not in t and "\\" not in t for t in terms):
        return [t.encode() for t in terms]
    return None


def _lines(f: BinaryIO) -> Iterator[tuple[bytes, int]]:
    """``(line, bytes read)``; an overlong line comes back empty but counted."""
    while True:
        line = f.readline(LINE_CAP)
        if not line:
            return
        if len(line) == LINE_CAP and not line.endswith(b"\n"):
            size = len(line)
            while rest := f.readline(LINE_CAP):
                size += len(rest)
                if rest.endswith(b"\n"):
                    break
            yield b"", size
            continue
        yield line, len(line)


def _fields(rec: dict[str, Any]) -> Iterator[tuple[str, str, Any, dict[str, Any]]]:
    """``(where, text, seq, extra)`` for each searchable field of a record."""
    kind = rec.get("kind")
    if kind == "event" and isinstance(rec.get("ev"), dict):
        ev = rec["ev"]
        seq = rec.get("seq") if isinstance(rec.get("seq"), int) else None
        extra = {"ts": rec.get("ts"), "turn": ev.get("turn")}
        kind = ev.get("type")
        if kind == "user_message" and isinstance(ev.get("text"), str):
            yield "user", ev["text"], seq, extra
        elif kind == "assistant_message" and isinstance(ev.get("text"), str):
            yield "assistant", ev["text"], seq, extra
        elif kind == "tool_call" and isinstance(ev.get("name"), str):
            yield "tool", ev["name"], seq, extra
    elif kind == "message" and isinstance(rec.get("message"), dict):
        msg = rec["message"]
        extra = {"ts": rec.get("ts"), "turn": None}
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        if msg.get("role") == "user" and content:
            yield "user", strip_reminders(content).strip(), None, extra
        elif msg.get("role") == "assistant":
            if content:
                yield "assistant", content, None, extra
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict) and isinstance(call.get("name"), str):
                    yield "tool", call["name"], None, extra


def snippet(text: str, term: str, width: int = SNIPPET) -> str:
    """One line of ``text`` around the first occurrence of ``term``."""
    low = text.lower()
    at = max(0, low.find(term))
    # lower() can change the length of a few characters; then the offset is
    # only close, which is all a snippet needs.
    at = min(at, len(text))
    start = max(0, at - width // 3)
    end = min(len(text), start + width)
    start = max(0, end - width)
    body = " ".join(text[start:end].split())
    return ("…" if start else "") + body + ("…" if end < len(text) else "")


def _scan(path: Path, terms: list[str], budget: _Budget, keep: int) -> _Found:
    """The hits in one log, from its events or, for a log without any, its messages."""
    needles = _needles(terms)
    events, messages = _Found(), _Found()
    transcript = False
    with path.open("rb") as f:
        for n, (line, size) in enumerate(_lines(f)):
            if n % _CHECK_EVERY == 0 and not budget.in_time():
                break
            if not budget.take(size):
                break
            if not transcript and any(tok in line for tok in _TRANSCRIPT_TOKENS):
                transcript = True
            if needles is not None:
                low = line.lower()
                if not all(needle in low for needle in needles):
                    continue
            # The session reader's own parser: a BOM, NUL padding or a torn
            # line costs that line and nothing more.
            for rec in parse(line, at_start=n == 0).records:
                found = events if rec.get("kind") == "event" else messages
                for where, text, seq, extra in _fields(rec):
                    if all(t in text.lower() for t in terms):
                        found.add({"where": where, "seq": seq,
                                   "snippet": snippet(text, terms[0]), **extra}, keep)
    return events if transcript else messages


def search_sessions(
    root: Path,
    query: str,
    *,
    include_archived: bool = False,
    limits: Limits | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Sessions of the project at ``root`` that match ``query``, newest first."""
    terms = parse_query(query)
    limits = limits or Limits()
    budget = _Budget(limits=limits, clock=clock, started=clock())
    sessions = [s for s in SessionStore.list_sessions(Path(root), include_archived=include_archived)
                if safe_conv_id(s.conv_id)]
    results: list[dict[str, Any]] = []
    scanned = 0
    for info in sessions:
        if len(results) >= limits.max_sessions:
            budget.stopped = "results"
            break
        if not budget.in_time():
            break
        try:
            found = _scan(info.path, terms, budget, limits.max_hits)
        except OSError:
            continue
        scanned += 1
        title = info.title if info.title != "(empty)" else ""
        title_match = bool(title) and all(t in title.lower() for t in terms)
        if title_match or found.count:
            results.append({
                "conv_id": info.conv_id, "title": info.title, "mtime": info.mtime,
                "model": info.model, "archived": info.archived,
                "title_match": title_match, "hit_count": found.count, "hits": found.hits,
            })
        if budget.stopped:
            break
    return {
        "query": str(query).strip(), "terms": terms, "results": results,
        "scanned": scanned, "sessions": len(sessions), "bytes": budget.spent,
        "stopped": budget.stopped,
    }
