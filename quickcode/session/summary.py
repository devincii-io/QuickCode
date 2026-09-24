"""What the session list says about one log, folded record by record.

A fold rather than a query so it can be carried forward: logs are append-only,
so the summary of a log that has grown is the old summary plus the new records,
which is what lets the listing index (``index.py``) read only the tail.
``SessionStore.title`` and ``is_empty`` are answered from the same fold, so the
list and the session can never disagree about a name.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Event types that carry the transcript itself. Their presence is what tells a
# session log apart from one written before the event log existed.
TRANSCRIPT_EVENT_TYPES = frozenset(
    {"user_message", "assistant_message", "tool_call", "tool_result"}
)

# Subagent artifacts are named ``{agent-name}-{n}.md`` from a per-conversation
# counter, so the filename alone says nothing about which session owns it —
# two sessions can both have produced an ``explore-1.md``. The only record of
# ownership is the offload marker the runner splices into the tool result
# ("…written to <path>…"), which lands verbatim in the session log. Matching it
# in the raw JSONL text handles both separators and the doubled backslashes
# JSON escaping leaves behind on Windows.
ARTIFACT_REF_RE = re.compile(r"artifacts[\\/]+([A-Za-z0-9][A-Za-z0-9._-]*\.md)")

TITLE_CHARS = 60


def _first_user_content(messages: Any) -> str | None:
    for raw in messages if isinstance(messages, list) else []:
        text = _user_content(raw)
        if text is not None:
            return text
    return None


def _user_content(raw: Any) -> str | None:
    if (isinstance(raw, dict) and raw.get("role") == "user"
            and isinstance(raw.get("content"), str) and raw["content"]):
        return raw["content"].strip()[:TITLE_CHARS]
    return None


@dataclass
class Summary:
    #: The last ``meta`` title, stripped. Empty when never set or cleared.
    chosen_title: str = ""
    #: What the first ``user_message`` event said -- what the user typed.
    first_user_event: str | None = None
    #: The first user message of the model context as resume would load it
    #: (after the last compaction). Only a fallback for logs with no events.
    first_user_message: str | None = None
    #: The first model a ``meta`` record named.
    model: str = ""
    messages: int = 0
    transcript_events: int = 0
    artifacts: list[str] = field(default_factory=list)

    def fold(self, records: list[dict[str, Any]]) -> None:
        for rec in records:
            kind = rec.get("kind")
            if kind == "meta":
                # The *last* title wins, not the first. Renaming is an append,
                # so a log renamed twice carries two titles, and reading the
                # first back would show the name the user just replaced.
                if "title" in rec:
                    self.chosen_title = str(rec["title"] or "").strip()
                if not self.model and rec.get("model"):
                    self.model = str(rec["model"])
            elif kind == "message":
                self.messages += 1
                if self.first_user_message is None:
                    self.first_user_message = _user_content(rec.get("message"))
            elif kind == "compaction" and isinstance(rec.get("messages"), list):
                # Compaction replaces the context resume loads, so the fallback
                # title moves with it.
                self.first_user_message = _first_user_content(rec["messages"])
            elif kind == "event":
                ev = rec.get("ev")
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("type")
                if kind in TRANSCRIPT_EVENT_TYPES:
                    self.transcript_events += 1
                if (kind == "user_message" and self.first_user_event is None
                        and ev.get("text")):
                    self.first_user_event = str(ev["text"]).strip()[:TITLE_CHARS]

    def add_artifacts(self, text: str) -> None:
        found = set(self.artifacts) | set(ARTIFACT_REF_RE.findall(text))
        self.artifacts = sorted(found)

    @property
    def title(self) -> str:
        # Empty is not a title: a session is opened with ``title=""``, and a
        # rename to nothing is a request to go back to the derived name, not to
        # display blank.
        if self.chosen_title:
            return self.chosen_title
        # The event before the message, because the event carries what the
        # user typed and the persisted message carries what the model was sent
        # -- which has `<system-reminder>` blocks spliced into it.
        if self.first_user_event is not None:
            return self.first_user_event
        # A turn interrupted before messages were persisted, or a log old
        # enough to predate user_message events, still has to produce a title.
        if self.first_user_message is not None:
            return self.first_user_message
        return "(empty)"

    @property
    def message_count(self) -> int:
        # An event-only session (no persisted message log) still has a real
        # transcript, so count that rather than showing "0 msgs" for it.
        return self.messages or self.transcript_events

    @property
    def empty(self) -> bool:
        return not self.messages and not self.transcript_events

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Any) -> Summary | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                chosen_title=str(raw.get("chosen_title") or ""),
                first_user_event=_opt_str(raw.get("first_user_event")),
                first_user_message=_opt_str(raw.get("first_user_message")),
                model=str(raw.get("model") or ""),
                messages=int(raw.get("messages") or 0),
                transcript_events=int(raw.get("transcript_events") or 0),
                artifacts=[str(a) for a in raw.get("artifacts") or []],
            )
        except (TypeError, ValueError):
            return None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
