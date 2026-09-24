"""The session list with many sessions on disk.

The sidebar asks for it on every refresh. Each row used to parse its whole log
up to five times -- once for the counts, again for the event fallback, again
for the title, and twice more inside the title's own fallbacks -- so a project
with a few hundred long sessions paid that on every refresh, for rows that had
not changed since the last one.
"""

from __future__ import annotations

import os

import pytest

from quickcode.providers.base import ChatMessage
from quickcode.session.store import SessionStore


def _session(root, conv_id: str, *, title: str = "", turns: int = 3) -> SessionStore:
    store = SessionStore(root, conv_id)
    store.append_meta(title=title, model="test/model")
    for i in range(turns):
        store.append_event({"type": "user_message", "text": f"question {i} in {conv_id}"})
        store.append_message(ChatMessage(role="user", content=f"question {i}"))
        store.append_event({"type": "assistant_message", "text": "answer", "finish_reason": "stop"})
        store.append_message(ChatMessage(role="assistant", content="answer"))
    return store


def _count_parses(monkeypatch) -> list[str]:
    parsed: list[str] = []
    real = SessionStore._iter_records

    def counting(self):
        parsed.append(self.conv_id)
        return real(self)

    monkeypatch.setattr(SessionStore, "_iter_records", counting)
    return parsed


def test_each_session_log_is_read_once_per_listing(tmp_path, monkeypatch):
    for i in range(5):
        _session(tmp_path, f"s{i:011d}")
    parsed = _count_parses(monkeypatch)

    infos = SessionStore.list_sessions(tmp_path)

    assert len(infos) == 5
    assert sorted(parsed) == sorted(i.conv_id for i in infos)


def test_an_unchanged_session_is_not_read_again(tmp_path, monkeypatch):
    for i in range(5):
        _session(tmp_path, f"s{i:011d}")
    first = SessionStore.list_sessions(tmp_path)
    parsed = _count_parses(monkeypatch)

    again = SessionStore.list_sessions(tmp_path)

    assert parsed == []
    assert [(i.conv_id, i.title, i.message_count) for i in again] == [
        (i.conv_id, i.title, i.message_count) for i in first
    ]


def test_a_session_that_changed_is_read_again_and_shows_the_change(tmp_path, monkeypatch):
    stores = [_session(tmp_path, f"s{i:011d}") for i in range(3)]
    SessionStore.list_sessions(tmp_path)
    parsed = _count_parses(monkeypatch)

    stores[1].rename("a new name")
    parsed.clear()  # rename reads its own log back to answer with the title
    infos = {i.conv_id: i for i in SessionStore.list_sessions(tmp_path)}

    assert parsed == [stores[1].conv_id]
    assert infos[stores[1].conv_id].title == "a new name"


def test_the_listing_says_what_the_per_session_readers_say(tmp_path):
    """One pass has to reach the answers ``title()`` and friends reach on their
    own, including their fallbacks."""
    named = _session(tmp_path, "named0000000", title="chosen")
    derived = _session(tmp_path, "derived00000")
    events_only = SessionStore(tmp_path, "events000000")
    events_only.append_meta(title="", model="m2")
    events_only.append_event({"type": "user_message", "text": "only an event"})
    messages_only = SessionStore(tmp_path, "messages0000")
    messages_only.append_meta(title="", model="m3")
    messages_only.append_message(ChatMessage(role="user", content="legacy question"))
    compacted = _session(tmp_path, "compacted000")
    compacted.append_compaction([ChatMessage(role="user", content="the summary seed")])
    empty = SessionStore(tmp_path, "empty0000000")
    empty.append_meta(title="", model="m4")

    infos = {i.conv_id: i for i in SessionStore.list_sessions(tmp_path)}
    for store in (named, derived, events_only, messages_only, compacted, empty):
        info = infos[store.conv_id]
        fresh = SessionStore(tmp_path, store.conv_id)
        assert info.title == fresh.title(), store.conv_id
    assert infos["named0000000"].title == "chosen"
    assert infos["events000000"].message_count == 1
    assert infos["messages0000"].model == "m3"
    assert infos["empty0000000"].title == "(empty)"


def test_archiving_moves_a_session_between_the_two_listings(tmp_path):
    store = _session(tmp_path, "s00000000000")
    assert [i.conv_id for i in SessionStore.list_sessions(tmp_path)] == [store.conv_id]
    store.archive()
    assert SessionStore.list_sessions(tmp_path) == []
    archived = SessionStore.list_sessions(tmp_path, include_archived=True)
    assert [(i.conv_id, i.archived) for i in archived] == [(store.conv_id, True)]


@pytest.mark.skipif(os.name == "nt", reason="st_ctime is the creation time on Windows")
def test_a_session_rewritten_to_the_same_size_and_time_is_still_reread(tmp_path):
    """Logs are append-only, so size and mtime are what normally move. A
    same-length rewrite with its mtime put back (and, on a filesystem that
    reuses inode numbers at once, the same inode) is still caught by ctime,
    which nothing can set back."""
    store = _session(tmp_path, "s00000000000", title="aaaa")
    before = os.stat(store.path)
    assert SessionStore.list_sessions(tmp_path)[0].title == "aaaa"
    text = store.path.read_text(encoding="utf-8").replace('"aaaa"', '"bbbb"')
    store.path.unlink()
    store.path.write_text(text, encoding="utf-8")
    os.utime(store.path, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert SessionStore.list_sessions(tmp_path)[0].title == "bbbb"
