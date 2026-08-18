"""JSONL session persistence: append-only conversation logs on disk.

Each conversation is one JSONL file under ``<root>/.quickcode/sessions/``.
Lines are one of:
  - ``{"kind": "message", ...}`` — a serialized ``ChatMessage`` (model context)
  - ``{"kind": "meta", ...}``    — free-form session metadata (title/model)
  - ``{"kind": "event", ...}``   — a UI/trace event (the append-only event log
    the web transcript replays; see server/serialization.py for shapes)

The event log is the source of truth for what the user *saw*; the message log
is the source of truth for what the model *sees* on resume.

Archival is a *move*, not a flag: an archived session's JSONL is relocated to
``<sessions>/archive/``. The listing glob is non-recursive, so archived logs
drop out of every listing — including one produced by a build that has never
heard of archiving — while the bytes stay untouched and the mtime survives.
Nothing has to be written into the log itself, so there is no way for an older
reader to misread a record it does not know.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.providers.base import ChatMessage
from quickcode.workspace import ensure_project_dir

SESSIONS_DIRNAME = Path(".quickcode") / "sessions"
ARCHIVE_DIRNAME = "archive"
TASKS_DIRNAME = Path(".quickcode") / "tasks"
ARTIFACTS_DIRNAME = Path(".quickcode") / "artifacts"

# Subagent artifacts are named ``{agent-name}-{n}.md`` from a per-conversation
# counter, so the filename alone says nothing about which session owns it —
# two sessions can both have produced an ``explore-1.md``. The only record of
# ownership is the offload marker the runner splices into the tool result
# ("…written to <path>…"), which lands verbatim in the session log. Matching it
# in the raw JSONL text handles both separators and the doubled backslashes
# JSON escaping leaves behind on Windows.
_ARTIFACT_REF_RE = re.compile(r"artifacts[\\/]+([A-Za-z0-9][A-Za-z0-9._-]*\.md)")

# Event types that carry the transcript itself. Their presence is what tells a
# session log apart from one written before the event log existed.
TRANSCRIPT_EVENT_TYPES = frozenset(
    {"user_message", "assistant_message", "tool_call", "tool_result"}
)

_REMINDER_RE = re.compile(r"\n*<system-reminder>.*?</system-reminder>", re.DOTALL)


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


def _events_from_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Project a model-context message list onto the trace-event vocabulary.

    Lossy by nature — a message log has no timings, no reasoning and no
    permission decisions — but it is the difference between a legacy session
    rendering its history and rendering nothing at all. Sequence numbers count
    *down* from zero so the synthesized prefix stays ordered and stays clear
    of the real log's positive numbering.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user" and msg.content:
            # The stored message carries the reminders that were spliced into
            # the turn; the transcript only ever showed what the user typed.
            text = _REMINDER_RE.sub("", msg.content).strip()
            out.append({"type": "user_message", "text": text or msg.content})
        elif msg.role == "assistant":
            if msg.content:
                out.append(
                    {
                        "type": "assistant_message",
                        "text": msg.content,
                        "reasoning": "",
                        "finish_reason": "tool_calls" if msg.tool_calls else "stop",
                    }
                )
            for tc in msg.tool_calls:
                out.append(
                    {
                        "type": "tool_call",
                        "id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "arguments": tc.get("arguments", ""),
                    }
                )
        elif msg.role == "tool":
            content = msg.content or ""
            out.append(
                {
                    "type": "tool_result",
                    "id": msg.tool_call_id or "",
                    "name": msg.name or "",
                    "content": content,
                    "is_error": content.startswith("[error]"),
                    "ms": 0,
                }
            )
    total = len(out)
    for i, ev in enumerate(out):
        ev["seq"] = i - total
        ev["ts"] = None
        ev["turn"] = 0
    return out


@dataclass
class SessionInfo:
    conv_id: str
    path: Path
    mtime: float
    title: str
    model: str
    message_count: int
    archived: bool = False


@dataclass
class PurgeResult:
    """What a delete actually removed from disk."""

    sessions: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class SessionStore:
    """Append-only JSONL persistence for one conversation."""

    def __init__(self, root: Path, conv_id: str | None = None) -> None:
        self.root = Path(root)
        self.conv_id = conv_id or uuid.uuid4().hex[:12]
        self.sessions_dir = self.root / SESSIONS_DIRNAME
        self.archive_dir = self.sessions_dir / ARCHIVE_DIRNAME
        self.active_path = self.sessions_dir / f"{self.conv_id}.jsonl"
        self.archived_path = self.archive_dir / f"{self.conv_id}.jsonl"
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        """Where this conversation's log lives right now.

        Archiving moves the file, so the store follows it: an archived session
        that gets resumed keeps appending to the same bytes instead of
        silently starting a second log under the same id.
        """
        if self.active_path.exists():
            return self.active_path
        if self.archived_path.exists():
            return self.archived_path
        return self.active_path

    @property
    def archived(self) -> bool:
        return not self.active_path.exists() and self.archived_path.exists()

    # ---- archival ----
    def archive(self) -> bool:
        """Move the log out of the default listing. False if there is nothing
        to archive (or it already is)."""
        if not self.active_path.exists():
            return False
        ensure_project_dir(self.root)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        if self.archived_path.exists():
            # Two logs for one id is a state we refuse to create; the caller
            # sees "already archived" rather than losing one of them.
            return False
        self.active_path.replace(self.archived_path)
        return True

    def unarchive(self) -> bool:
        """Inverse of :meth:`archive`. False if it was not archived."""
        if not self.archived_path.exists() or self.active_path.exists():
            return False
        ensure_project_dir(self.root)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.archived_path.replace(self.active_path)
        return True

    def _scan_last_seq(self) -> int:
        last = 0
        for rec in self._iter_records():
            if rec.get("kind") == "event" and isinstance(rec.get("seq"), int):
                last = max(last, rec["seq"])
        return last

    # ---- writing ----
    def _append_line(self, obj: dict[str, Any]) -> None:
        target = self.path
        # The first line of the first session is what creates ``.quickcode/``
        # in a fresh project, so it is also where the directory gets the
        # ``.gitignore`` that stops this log from being committed.
        ensure_project_dir(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
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

    def replay_events(self) -> list[dict[str, Any]]:
        """The event stream a freshly attached client should replay.

        Normally that is just ``load_events()``. Sessions written before the
        event log existed hold only ``message`` records, so their transcript
        would replay empty. (The headless CLI used to write such logs too; it
        records events like any other run now, but old ones are still out
        there and still have to open.) Those get
        their messages projected onto the event vocabulary, with negative
        sequence numbers so they can never collide with (or be deduped
        against) the real log. A session that already carries transcript
        events of its own is left exactly as it is.
        """
        events = self.load_events()
        if any(e.get("type") in TRANSCRIPT_EVENT_TYPES for e in events):
            return events
        synthesized = _events_from_messages(self.load_messages())
        return synthesized + events

    def meta(self) -> dict[str, Any]:
        """Every ``meta`` record merged, later writes winning.

        Used on resume to recover what the session was started with -- its
        preset above all, since a conversation must keep the composition it
        was told it had.
        """
        merged: dict[str, Any] = {}
        for rec in self._iter_records():
            if rec.get("kind") != "meta":
                continue
            for key, value in rec.items():
                if key not in ("kind", "ts") and value not in (None, ""):
                    merged[key] = value
        return merged

    def title(self) -> str:
        for rec in self._iter_records():
            if rec.get("kind") == "meta" and rec.get("title"):
                return str(rec["title"])
        # The event before the message, because the event carries what the user
        # typed and the persisted message carries what the model was sent —
        # which has `<system-reminder>` blocks spliced into it. Titling a
        # session with the runtime's own scaffolding, rather than the sentence
        # the person wrote, puts internals in the session list.
        for ev in self.load_events():
            if ev.get("type") == "user_message" and ev.get("text"):
                return str(ev["text"]).strip()[:60]
        # A turn interrupted before messages were persisted, or a log old
        # enough to predate user_message events, still has to produce a title.
        for msg in self.load_messages():
            if msg.role == "user" and msg.content:
                return msg.content.strip()[:60]
        return "(empty)"

    def is_empty(self) -> bool:
        """True only when this log holds no transcript whatsoever.

        A session with zero ``message`` records is *not* automatically empty:
        a turn interrupted before ``_persist_new_messages()`` ran leaves the
        message log bare while the event log — written per event — still holds
        the whole conversation. Both logs have to be silent before a session
        can be swept up as abandoned; anything less would be data loss.
        """
        for rec in self._iter_records():
            kind = rec.get("kind")
            if kind == "message":
                return False
            if kind == "event":
                ev = rec.get("ev")
                if isinstance(ev, dict) and ev.get("type") in TRANSCRIPT_EVENT_TYPES:
                    return False
        return True

    def artifact_refs(self) -> set[str]:
        """Names of subagent artifacts this session's log points at."""
        path = self.path
        if not path.exists():
            return set()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return set()
        return set(_ARTIFACT_REF_RE.findall(text))

    # ---- listing ----
    @classmethod
    def _info(cls, root: Path, path: Path, *, archived: bool) -> SessionInfo | None:
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
            if not message_count:
                # Same fallback as title(): an event-only session (no
                # persisted message log) still has a real transcript, so
                # count that rather than showing "0 msgs" for it.
                message_count = sum(
                    1 for ev in store.load_events()
                    if ev.get("type") in TRANSCRIPT_EVENT_TYPES
                )
            title = store.title()
        except OSError:
            return None
        return SessionInfo(
            conv_id=conv_id,
            path=path,
            mtime=mtime,
            title=title,
            model=model,
            message_count=message_count,
            archived=archived,
        )

    @classmethod
    def list_sessions(
        cls, root: Path, *, include_archived: bool = False, archived_only: bool = False
    ) -> list[SessionInfo]:
        """Sessions newest first. Archived logs are excluded by default; the
        glob is non-recursive, so the archive subdirectory costs nothing."""
        sessions_dir = Path(root) / SESSIONS_DIRNAME
        infos: list[SessionInfo] = []
        if not archived_only and sessions_dir.is_dir():
            for path in sessions_dir.glob("*.jsonl"):
                info = cls._info(root, path, archived=False)
                if info is not None:
                    infos.append(info)
        if include_archived or archived_only:
            archive_dir = sessions_dir / ARCHIVE_DIRNAME
            if archive_dir.is_dir():
                for path in archive_dir.glob("*.jsonl"):
                    info = cls._info(root, path, archived=True)
                    if info is not None:
                        infos.append(info)
        infos.sort(key=lambda s: s.mtime, reverse=True)
        return infos

    @classmethod
    def empty_sessions(cls, root: Path) -> list[str]:
        """Ids of non-archived sessions holding no transcript at all.

        Archived ones are left out on purpose: archiving is a deliberate act,
        and a sweep for abandoned logs must not undo it.
        """
        out = []
        for info in cls.list_sessions(root):
            if info.message_count == 0 and cls(root, info.conv_id).is_empty():
                out.append(info.conv_id)
        return out

    @classmethod
    def most_recent(cls, root: Path) -> str | None:
        sessions = cls.list_sessions(root)
        if not sessions:
            return None
        return sessions[0].conv_id


def purge_sessions(root: Path, conv_ids: Iterable[str]) -> PurgeResult:
    """Delete sessions and everything on disk that belonged only to them.

    A session owns three things: its JSONL (archived or not), its task board
    under ``.quickcode/tasks/<conv_id>/``, and the subagent artifacts its log
    references. Artifacts are shared namespace — the id counter restarts per
    conversation — so one is removed only when no *surviving* session still
    points at it.
    """
    root = Path(root)
    result = PurgeResult()
    doomed_refs: set[str] = set()

    for conv_id in conv_ids:
        store = SessionStore(root, conv_id)
        if not store.path.exists():
            result.missing.append(conv_id)
            continue
        doomed_refs |= store.artifact_refs()
        try:
            store.path.unlink()
        except OSError:
            result.missing.append(conv_id)
            continue
        result.sessions.append(conv_id)
        board_dir = root / TASKS_DIRNAME / conv_id
        if board_dir.is_dir():
            shutil.rmtree(board_dir, ignore_errors=True)
            result.boards.append(conv_id)

    if doomed_refs:
        keep: set[str] = set()
        for info in SessionStore.list_sessions(root, include_archived=True):
            keep |= SessionStore(root, info.conv_id).artifact_refs()
        artifacts_dir = root / ARTIFACTS_DIRNAME
        for name in sorted(doomed_refs - keep):
            target = artifacts_dir / name
            # Guard the join: the name comes out of a log, so it is untrusted.
            if target.parent != artifacts_dir or not target.is_file():
                continue
            try:
                target.unlink()
            except OSError:
                continue
            result.artifacts.append(name)
    return result
