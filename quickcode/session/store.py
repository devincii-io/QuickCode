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

import contextlib
import datetime
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.providers.base import ChatMessage
from quickcode.workspace import ensure_project_dir

PROJECT_DIRNAME = ".quickcode"
SESSIONS_DIRNAME = Path(PROJECT_DIRNAME) / "sessions"
ARCHIVE_DIRNAME = "archive"
TASKS_DIRNAME = Path(PROJECT_DIRNAME) / "tasks"
ARTIFACTS_DIRNAME = Path(PROJECT_DIRNAME) / "artifacts"

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

# The longest name a rename may give a session. Titles derived from the first
# user message are cut at 60; a chosen one may be a sentence, because it is the
# only handle on a conversation whose first message says "continue" — but it is
# drawn in a chip, a tab and a menu row, all of which truncate, so a paragraph
# would only be a paragraph on disk.
MAX_TITLE = 200

# The shape a conversation id is allowed to have. The server enforces the same
# rule on the way in (server/app.py `_CONV_ID_RE`), but ids also come *off the
# disk* -- `list_sessions` and `empty_sessions` derive them from filenames --
# so the last line of defence belongs here, next to the code that deletes.
_SAFE_CONV_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def safe_conv_id(conv_id: str) -> bool:
    """Whether ``conv_id`` may be used to build a path under the project."""
    return bool(conv_id) and _SAFE_CONV_ID.fullmatch(str(conv_id)) is not None


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
        # Set by ``hold`` for a conversation nobody has said anything in yet;
        # records pile up here until ``release``. See ``hold``.
        self._holding = False
        self._deferred: list[dict[str, Any]] = []

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
    def begin(self, **fields: Any) -> None:
        """Open a conversation: record its first ``meta`` line, write nothing.

        Opening a project opens a conversation, and a new conversation wrote
        its ``meta`` record and its ``system_prompt`` event immediately. So
        merely *starting the app* left a session on disk: a user who had opened
        QuickCode seven times found seven empty sessions in their list. The
        "clean up N empty" button in the UI is a workaround for this, not a
        feature anyone asked for.

        Held rather than dropped, because those records are not optional. The
        meta line carries the model, the preset and the resolved composition,
        and the trajectory and resume both read it as the first line of the
        file. They are flushed ahead of whatever is said first, so the on-disk
        order is exactly what it has always been -- a reader cannot tell the
        difference. A conversation nobody says anything in simply never becomes
        a file.

        Released by anything that is an act rather than a side effect: a chat
        message, the user speaking, a rename, a composition switch. Ambient
        events emitted because a window happens to be open -- the system
        prompt, a mode announcement -- are not that.
        """
        self._holding = True
        self._deferred.append({"kind": "meta", **fields})

    def release(self) -> None:
        """Stop holding, writing anything held so far in the order it arrived."""
        if not self._holding:
            return
        self._holding = False
        if self._deferred:
            pending, self._deferred = self._deferred, []
            self._write_lines(pending)

    def _append_line(self, obj: dict[str, Any]) -> None:
        if self._holding:
            self._deferred.append(obj)
            return
        self._write_lines([obj])

    def _write_lines(self, objs: list[dict[str, Any]]) -> None:
        target = self.path
        # The first line of the first session is what creates ``.quickcode/``
        # in a fresh project, so it is also where the directory gets the
        # ``.gitignore`` that stops this log from being committed.
        ensure_project_dir(self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            for obj in objs:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def append_message(self, msg: ChatMessage) -> None:
        self.release()
        self._append_line(
            {
                "kind": "message",
                "ts": datetime.datetime.now().isoformat(),
                "message": message_to_dict(msg),
            }
        )

    def append_compaction(self, messages: Iterable[ChatMessage]) -> None:
        """Record that the model's context was rebuilt, and what it became.

        Compaction replaces the history wholesale with a summary plus a tail,
        and until this existed that only happened in memory: the log kept every
        original message, so *reopening a compacted session reloaded the entire
        pre-compaction transcript*. The work was undone, silently, and the
        first request after the resume carried exactly the context compaction
        had been run to avoid -- at full price, or straight into a
        context-length rejection.

        Appended like everything else here; nothing is rewritten. A reader that
        has never heard of this record ignores it and loads the messages as
        before, which is the behaviour it has today.
        """
        self._append_line(
            {
                "kind": "compaction",
                "ts": datetime.datetime.now().isoformat(),
                "messages": [message_to_dict(m) for m in messages],
            }
        )

    def append_meta(self, **fields: Any) -> None:
        # Metadata is written when somebody asks for it -- a rename, a
        # composition switch. Those are acts, so they make the session real
        # even if nothing has been said in it yet. Only the opening record,
        # which `begin` queues directly, is held.
        self.release()
        self._append_line({"kind": "meta", **fields})

    def rename(self, title: str) -> str:
        """Give this session a name of its own; returns the name it now shows.

        An append like every other write here, which is what makes it safe on a
        conversation that is running: nothing is rewritten and nothing moves, so
        the live writer keeps appending to the same file behind it. (Archiving
        and deleting cannot say that — they move or unlink the log — which is
        why those two refuse a live session and this does not.)

        Whitespace is collapsed because the title is rendered on one line in
        three places, and a blank title is a request to go back to the derived
        name rather than to display nothing.
        """
        cleaned = " ".join(str(title).split())[:MAX_TITLE]
        self.append_meta(title=cleaned)
        return self.title()

    def append_event(self, ev: dict[str, Any]) -> int:
        """Append one trace event; returns the sequence number assigned.

        The timestamp is stamped *into* ``ev`` as well as onto the record. The
        caller broadcasts this same dict to attached clients, and stamping only
        the record left every live event on the wire with no wall clock at all
        -- so a running turn drew as a pile of events on one instant, and only
        a reload (which replays from disk, where ``load_events`` folds the
        record's ``ts`` back in) put them where they actually happened.
        """
        if self._next_seq is None:
            self._next_seq = self._scan_last_seq() + 1
        seq = self._next_seq
        self._next_seq += 1
        ts = ev["ts"] = datetime.datetime.now().isoformat()
        # The user saying something is what turns an open window into a
        # session. Everything before it was the app getting ready.
        if ev.get("type") == "user_message":
            self.release()
        self._append_line({"kind": "event", "seq": seq, "ts": ts, "ev": ev})
        return seq

    # ---- reading ----
    def _iter_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except (json.JSONDecodeError, ValueError):
                        continue
        # Held records are part of this session as far as every reader is
        # concerned. Only the *write* is deferred: a client that attaches to a
        # conversation before anything is said still replays its system prompt,
        # exactly as it did when opening the window created a file.
        records.extend(self._deferred)
        return records

    def load_messages(self) -> list[ChatMessage]:
        """The model context to resume with.

        A ``compaction`` record replaces everything before it: it *is* the
        history as of that moment, so messages logged earlier are the ones
        compaction removed and must not come back. Messages appended after it
        are the turns that followed and are kept. The last such record wins,
        because a long session compacts more than once.
        """
        messages: list[ChatMessage] = []
        for rec in self._iter_records():
            kind = rec.get("kind")
            if kind == "compaction" and isinstance(rec.get("messages"), list):
                rebuilt: list[ChatMessage] = []
                for raw in rec["messages"]:
                    try:
                        rebuilt.append(message_from_dict(raw))
                    except (KeyError, TypeError):
                        continue
                messages = rebuilt
            elif kind == "message" and "message" in rec:
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
        # The *last* meta title wins, not the first. Renaming is an append —
        # there is no other kind of write this format has — so a log that has
        # been renamed twice carries two titles, and reading the first one back
        # would show the name the user just replaced. Empty is not a title: a
        # session is opened with ``title=""``, and a rename to nothing is a
        # request to go back to the derived name below, not to display blank.
        chosen = ""
        for rec in self._iter_records():
            if rec.get("kind") == "meta" and "title" in rec:
                chosen = str(rec["title"] or "").strip()
        if chosen:
            return chosen
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
            # Ids here come from filenames on disk, not from the server's
            # validated routes, so a file named `...jsonl` offered `..` as a
            # sweepable session. Nothing downstream should have trusted it —
            # it now also never gets that far.
            if not safe_conv_id(info.conv_id):
                continue
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
        if not safe_conv_id(conv_id):
            # `..` is the one that matters: the board path below is
            # `.quickcode/tasks/<conv_id>`, and `.quickcode/tasks/..` is
            # `.quickcode` itself, so one sweep took the whole directory —
            # every other session, every board, every artifact and the project
            # settings. A file named `...jsonl` in the sessions directory was
            # enough to put `..` on the list, and "clean up empty sessions" is
            # one click.
            result.missing.append(conv_id)
            continue
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
        # Belt and braces beside the id check above: whatever the id said, the
        # directory being removed has to be a child of the tasks directory.
        if board_dir.parent != root / TASKS_DIRNAME:
            continue
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


# ---- deleting everything QuickCode wrote for one project ----
#
# This is the only code in the app that removes a whole directory tree inside a
# user's project, so the containment check is written before the delete and is
# the part the tests weigh most. The rule it enforces has no exceptions:
#
#   the only removable path is ``<realpath(project root)>/.quickcode``, it must
#   be a real directory, and it must not be a symlink or a junction.
#
# Everything else in the project — the source code, the .git directory, a
# sibling that merely looks like ours — is out of reach by construction, and a
# path that cannot be proved to be the one directory raises instead of guessing.


@dataclass
class ProjectPurgeResult:
    """What deleting a project's QuickCode data actually did."""

    #: The resolved directory that was (or would have been) removed.
    path: str
    #: False when there was nothing there — not an error, just nothing to do.
    existed: bool = False
    removed: bool = False


def project_data_dir(root: str | os.PathLike[str]) -> Path:
    """The one directory a project purge is allowed to remove.

    Raises ``ValueError`` rather than returning something unproven. The check is
    the same containment idiom ``server.gitinfo._safe_rel`` uses — resolve both
    ends with ``realpath`` and compare — with one extra refusal: a symlinked (or
    junctioned) ``.quickcode`` is rejected outright rather than followed, because
    following it would delete a tree that is, by definition, somewhere else.

    Note what this does *not* do: it never returns the project root itself. The
    name it appends is a non-empty constant, so the answer is always strictly
    below the root, and ``purge_project_data`` re-checks that before unlinking.
    """
    try:
        base = Path(os.path.realpath(root))
    except (OSError, ValueError) as e:  # pragma: no cover - platform-specific
        raise ValueError(f"cannot resolve project directory: {root}") from e
    if not base.is_dir():
        raise ValueError(f"not a project directory: {root}")
    candidate = base / PROJECT_DIRNAME
    if candidate.is_symlink():
        raise ValueError(
            f"{candidate} is a link, not a directory; refusing to delete what it points at"
        )
    # ``is_symlink`` is not enough on Windows: a *directory junction* answers
    # False there while still pointing somewhere else entirely. The realpath
    # comparison below is what actually catches it, and is therefore the check
    # that carries the guarantee — the explicit symlink refusal above only
    # exists so the common case says why in words.
    target = Path(os.path.realpath(candidate))
    if target.parent != base or target.name != PROJECT_DIRNAME or target == base:
        raise ValueError(
            f"{candidate} does not resolve to a directory inside {base}; refusing to delete it"
        )
    return target


def project_data_summary(root: str | os.PathLike[str]) -> dict[str, Any]:
    """What a purge would remove, for the dialog that has to name it.

    Counts only; nothing here reads a transcript. ``exists: false`` is the
    ordinary answer for a project that was opened once and never used.
    """
    target = project_data_dir(root)
    out: dict[str, Any] = {
        "path": str(target),
        "exists": target.is_dir(),
        "sessions": 0,
        "archived": 0,
        "boards": 0,
        "artifacts": 0,
        "bytes": 0,
        "entries": [],
    }
    if not out["exists"]:
        return out
    base = Path(os.path.realpath(root))
    sessions_dir = base / SESSIONS_DIRNAME
    with contextlib.suppress(OSError):
        out["sessions"] = sum(1 for _ in sessions_dir.glob("*.jsonl"))
    with contextlib.suppress(OSError):
        out["archived"] = sum(1 for _ in (sessions_dir / ARCHIVE_DIRNAME).glob("*.jsonl"))
    with contextlib.suppress(OSError):
        out["boards"] = sum(1 for p in (base / TASKS_DIRNAME).iterdir() if p.is_dir())
    with contextlib.suppress(OSError):
        out["artifacts"] = sum(1 for _ in (base / ARTIFACTS_DIRNAME).glob("*.md"))
    with contextlib.suppress(OSError):
        out["entries"] = sorted(p.name for p in target.iterdir())
    total = 0
    for dirpath, _dirs, files in os.walk(target):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(dirpath, name))
    out["bytes"] = total
    return out


def _is_reparse_point(path: Path) -> bool:
    """Whether ``path`` is a link of any kind, junctions included.

    ``Path.is_symlink()`` answers False for a Windows directory junction, which
    is exactly the case that matters here, so the reparse-point attribute is
    checked directly where the platform offers it.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attrs and reparse and attrs & reparse)


def _first_reparse_point(root: Path) -> Path | None:
    """The first link found anywhere under ``root``, or None. Never follows one."""
    for parent, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            candidate = Path(parent) / name
            if _is_reparse_point(candidate):
                return candidate
        # A junction answers True for is_dir(), so os.walk would descend into
        # it on the next iteration; drop them before it gets the chance.
        dirnames[:] = [d for d in dirnames if not _is_reparse_point(Path(parent) / d)]
    return None


def purge_project_data(root: str | os.PathLike[str]) -> ProjectPurgeResult:
    """Delete ``<project>/.quickcode`` and nothing else.

    Raises ``ValueError`` for any path that cannot be proved to be exactly that
    directory, and ``OSError`` if the removal itself fails. A project with no
    ``.quickcode`` is not an error: the result simply says nothing existed.
    """
    target = project_data_dir(root)
    result = ProjectPurgeResult(path=str(target))
    if not target.exists():
        return result
    result.existed = True
    if not target.is_dir():
        raise ValueError(f"{target} is not a directory")
    escape = _first_reparse_point(target)
    if escape is not None:
        # The containment check above proves `.quickcode` itself is inside the
        # project. It says nothing about what is inside `.quickcode`, and
        # `shutil.rmtree` recurses into a Windows directory junction — which
        # reports as an ordinary directory, not a link — so a junction in here
        # would carry the delete out of the project entirely. QuickCode never
        # creates one; refusing costs nothing and the alternative is silent
        # data loss somewhere the user never named.
        raise ValueError(
            f"{escape} points outside {target}; refusing to delete this directory. "
            "Remove that link yourself, then try again."
        )
    # Belt and braces: the last thing checked before the tree goes is that we
    # are still strictly below the project root and not standing on it.
    base = Path(os.path.realpath(root))
    if target == base or target.parent != base:  # pragma: no cover - unreachable
        raise ValueError(f"refusing to delete {target}")
    shutil.rmtree(target)
    result.removed = not target.exists()
    return result
