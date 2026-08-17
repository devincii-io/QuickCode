"""JSONL session persistence: append-only conversation logs on disk.

Each conversation is one JSONL file under ``<root>/.quickcode/sessions/``.
Lines are one of:
  - ``{"kind": "message", ...}`` — a serialized ``ChatMessage`` (model context)
  - ``{"kind": "meta", ...}``    — free-form session metadata (title/model)
  - ``{"kind": "event", ...}``   — a UI/trace event (the append-only event log
    the web transcript replays; see server/serialization.py for shapes)

The event log is the source of truth for what the user *saw*; the message log
is the source of truth for what the model *sees* on resume.
"""

from __future__ import annotations

import datetime
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.providers.base import ChatMessage

SESSIONS_DIRNAME = Path(".quickcode") / "sessions"


def message_to_dict(msg: ChatMessage) -> dict[str, Any]:
    """Serialize a ``ChatMessage`` to a plain JSON-able dict."""
    return {
        "role": msg.role,
        "content": msg.content,
        "tool_calls": msg.tool_calls,
        "tool_call_id": msg.tool_call_id,
        "name": msg.name,
        "cache_control": msg.cache_control,
    }


def message_from_dict(d: dict[str, Any]) -> ChatMessage:
    """Inverse of ``message_to_dict``."""
    return ChatMessage(
        role=d["role"],
        content=d.get("content", ""),
        tool_calls=d.get("tool_calls") or [],
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
        cache_control=d.get("cache_control", False),
    )


@dataclass
class SessionInfo:
    conv_id: str
    path: Path
    mtime: float
    title: str
    model: str
    message_count: int


class SessionStore:
    """Append-only JSONL persistence for one conversation."""

    def __init__(self, root: Path, conv_id: str | None = None) -> None:
        self.root = Path(root)
        self.conv_id = conv_id or uuid.uuid4().hex[:12]
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self.path = self.sessions_dir / f"{self.conv_id}.jsonl"
        self._next_seq: int | None = None

    def _scan_last_seq(self) -> int:
        last = 0
        for rec in self._iter_records():
            if rec.get("kind") == "event" and isinstance(rec.get("seq"), int):
                last = max(last, rec["seq"])
        return last

    # ---- writing ----
    def _append_line(self, obj: dict[str, Any]) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def append_message(self, msg: ChatMessage) -> None:
        self._append_line(
            {
                "kind": "message",
                "ts": datetime.datetime.now().isoformat(),
                "message": message_to_dict(msg),
            }
        )

    def append_meta(self, **fields: Any) -> None:
        self._append_line({"kind": "meta", **fields})

    def append_event(self, ev: dict[str, Any]) -> int:
        """Append one trace event; returns the sequence number assigned."""
        if self._next_seq is None:
            self._next_seq = self._scan_last_seq() + 1
        seq = self._next_seq
        self._next_seq += 1
        self._append_line(
            {
                "kind": "event",
                "seq": seq,
                "ts": datetime.datetime.now().isoformat(),
                "ev": ev,
            }
        )
        return seq

    # ---- reading ----
    def _iter_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        return records

    def load_messages(self) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for rec in self._iter_records():
            if rec.get("kind") == "message" and "message" in rec:
                try:
                    messages.append(message_from_dict(rec["message"]))
                except (KeyError, TypeError):
                    continue
        return messages

    def load_events(self) -> list[dict[str, Any]]:
        """All trace events, oldest first, with ``seq``/``ts`` folded in."""
        events: list[dict[str, Any]] = []
        for rec in self._iter_records():
            if rec.get("kind") == "event" and isinstance(rec.get("ev"), dict):
                ev = dict(rec["ev"])
                ev["seq"] = rec.get("seq")
                ev["ts"] = rec.get("ts")
                events.append(ev)
        return events

    def title(self) -> str:
        for rec in self._iter_records():
            if rec.get("kind") == "meta" and rec.get("title"):
                return str(rec["title"])
        for msg in self.load_messages():
            if msg.role == "user" and msg.content:
                text = msg.content.strip()
                return text[:60]
        return "(empty)"

    # ---- listing ----
    @classmethod
    def list_sessions(cls, root: Path) -> list[SessionInfo]:
        sessions_dir = Path(root) / SESSIONS_DIRNAME
        if not sessions_dir.is_dir():
            return []
        infos: list[SessionInfo] = []
        for path in sessions_dir.glob("*.jsonl"):
            conv_id = path.stem
            try:
                store = cls(root, conv_id=conv_id)
                mtime = path.stat().st_mtime
                model = ""
                message_count = 0
                for rec in store._iter_records():
                    kind = rec.get("kind")
                    if kind == "meta" and not model and rec.get("model"):
                        model = str(rec["model"])
                    elif kind == "message":
                        message_count += 1
                title = store.title()
            except OSError:
                continue
            infos.append(
                SessionInfo(
                    conv_id=conv_id,
                    path=path,
                    mtime=mtime,
                    title=title,
                    model=model,
                    message_count=message_count,
                )
            )
        infos.sort(key=lambda s: s.mtime, reverse=True)
        return infos

    @classmethod
    def most_recent(cls, root: Path) -> str | None:
        sessions = cls.list_sessions(root)
        if not sessions:
            return None
        return sessions[0].conv_id
