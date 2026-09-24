"""The session list reads each log once, then only what was appended.

Listing parsed every log in full, three or four times over, to show a title
and a count: seconds per refresh for a few hundred sessions. The index beside
the logs is a cache -- these tests pin both halves of that: it saves the
reading, and it never says anything a fresh read of the logs would not.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quickcode.providers.base import ChatMessage
from quickcode.session import index as index_module
from quickcode.session.store import SessionStore, purge_sessions


def _sessions(root: Path) -> Path:
    return root / ".quickcode" / "sessions"


def _rows(root: Path, **kw) -> dict[str, tuple[str, str, int]]:
    return {
        s.conv_id: (s.title, s.model, s.message_count)
        for s in SessionStore.list_sessions(root, **kw)
    }


def _fresh_rows(root: Path, **kw) -> dict[str, tuple[str, str, int]]:
    (_sessions(root) / index_module.INDEX_NAME).unlink(missing_ok=True)
    return _rows(root, **kw)


@pytest.fixture
def reads(monkeypatch):
    """Every byte range the index parses, as (log name, first byte)."""
    seen: list[tuple[str, int]] = []
    real_build = index_module.SessionIndex._build
    real_extend = index_module.SessionIndex._extend

    def build(self, path, st):
        seen.append((path.name, 0))
        return real_build(self, path, st)

    def extend(self, entry, path, st):
        seen.append((path.name, entry.offset))
        return real_extend(self, entry, path, st)

    monkeypatch.setattr(index_module.SessionIndex, "_build", build)
    monkeypatch.setattr(index_module.SessionIndex, "_extend", extend)
    return seen


def _session(root: Path, conv_id: str, *texts: str, model: str = "m") -> SessionStore:
    store = SessionStore(root, conv_id)
    store.append_meta(title="", model=model)
    for text in texts:
        store.append_event({"type": "user_message", "text": text})
        store.append_message(ChatMessage(role="user", content=text))
    return store


def test_an_unchanged_log_is_not_read_again(tmp_path, reads):
    _session(tmp_path, "a", "first")
    _session(tmp_path, "b", "second")
    first = _rows(tmp_path)
    assert sorted(reads) == [("a.jsonl", 0), ("b.jsonl", 0)]
    reads.clear()
    assert _rows(tmp_path) == first
    assert reads == []


def test_a_grown_log_is_read_from_where_the_index_stopped(tmp_path, reads):
    store = _session(tmp_path, "a", "first")
    size = store.path.stat().st_size
    _rows(tmp_path)
    reads.clear()

    store.append_message(ChatMessage(role="assistant", content="answer"))
    store.rename("A name")
    assert _rows(tmp_path) == {"a": ("A name", "m", 2)}
    assert reads == [("a.jsonl", size)]
    assert _rows(tmp_path) == _fresh_rows(tmp_path)


def test_the_index_never_disagrees_with_the_logs(tmp_path):
    _session(tmp_path, "plain", "hello there", model="m1")
    renamed = _session(tmp_path, "renamed", "original")
    renamed.rename("Chosen")
    cleared = _session(tmp_path, "cleared", "derived")
    cleared.rename("Temp")
    cleared.rename("")
    events_only = SessionStore(tmp_path, "events-only")
    events_only.append_event({"type": "user_message", "text": "no messages persisted"})
    SessionStore(tmp_path, "empty").append_meta(title="", model="m")
    legacy = SessionStore(tmp_path, "legacy")
    legacy.append_message(ChatMessage(role="user", content="  predates events  "))
    compacted = SessionStore(tmp_path, "compacted")
    compacted.append_message(ChatMessage(role="user", content="gone"))
    compacted.append_compaction([ChatMessage(role="user", content="<summary>")])

    listed = {s.conv_id: s for s in SessionStore.list_sessions(tmp_path)}
    for conv_id, info in listed.items():
        store = SessionStore(tmp_path, conv_id)
        assert info.title == store.title(), conv_id
    assert _rows(tmp_path) == _fresh_rows(tmp_path)
    assert listed["cleared"].title == "derived"
    assert listed["events-only"].message_count == 1
    assert listed["compacted"].title == "<summary>"
    assert SessionStore.empty_sessions(tmp_path) == ["empty"]


def test_a_damaged_or_foreign_index_is_rebuilt(tmp_path):
    _session(tmp_path, "a", "first")
    expected = _rows(tmp_path)
    index = _sessions(tmp_path) / index_module.INDEX_NAME
    for junk in (b"{not json", b"[]", json.dumps({"version": 999}).encode(),
                 json.dumps({"version": 1, "sessions": {"a": {"size": "x"}}}).encode()):
        index.write_bytes(junk)
        assert _rows(tmp_path) == expected
    assert json.loads(index.read_text(encoding="utf-8"))["version"] == index_module.VERSION


def test_a_log_rewritten_underneath_the_index_is_read_from_scratch(tmp_path, reads):
    store = _session(tmp_path, "a", "first question", "second")
    _rows(tmp_path)
    # An editor round trip: every line ending changes, so the bytes before the
    # old offset are not the ones the index saw and nothing folded is kept.
    raw = store.path.read_bytes()
    store.path.write_bytes(raw.replace(b"first question", b"edited").replace(b"\n", b"\r\n"))
    reads.clear()
    assert _rows(tmp_path)["a"][0] == "edited"
    assert reads[-1] == ("a.jsonl", 0)
    # Cut shorter than what was folded: likewise.
    store.path.write_bytes(raw[: len(raw) // 3])
    reads.clear()
    assert _rows(tmp_path) == _fresh_rows(tmp_path)
    assert reads[0] == ("a.jsonl", 0)


def test_deleted_and_archived_logs_leave_the_index(tmp_path):
    _session(tmp_path, "a", "one")
    _session(tmp_path, "b", "two")
    _rows(tmp_path, include_archived=True)
    SessionStore(tmp_path, "a").archive()
    purge_sessions(tmp_path, ["b"])
    assert _rows(tmp_path) == {}
    assert _rows(tmp_path, archived_only=True) == {"a": ("one", "m", 1)}
    saved = json.loads((_sessions(tmp_path) / index_module.INDEX_NAME).read_text("utf-8"))
    assert sorted(saved["sessions"]) == ["archive/a"]


def test_the_index_is_not_a_session(tmp_path):
    _session(tmp_path, "a", "one")
    _rows(tmp_path)
    assert (_sessions(tmp_path) / index_module.INDEX_NAME).is_file()
    assert [s.conv_id for s in SessionStore.list_sessions(tmp_path)] == ["a"]
    assert SessionStore.most_recent(tmp_path) == "a"


def test_an_unwritable_index_still_lists(tmp_path, monkeypatch):
    _session(tmp_path, "a", "one")

    def refuse(*a, **k):
        raise PermissionError("read-only project")

    monkeypatch.setattr(index_module, "atomic_write_text", refuse)
    assert _rows(tmp_path) == {"a": ("one", "m", 1)}


def test_purge_keeps_an_artifact_referenced_only_in_the_index_tail(tmp_path):
    # The keep-set comes from the index now; an artifact reference appended
    # after the last listing must still protect the file.
    artifacts = tmp_path / ".quickcode" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "explore-1.md").write_text("report", encoding="utf-8")
    doomed = _session(tmp_path, "doomed", "x")
    doomed.append_message(ChatMessage(role="tool", content="written to artifacts/explore-1.md",
                                      tool_call_id="c", name="agent"))
    survivor = _session(tmp_path, "survivor", "y")
    _rows(tmp_path)
    survivor.append_message(ChatMessage(role="tool", content="see artifacts/explore-1.md",
                                        tool_call_id="c", name="agent"))
    assert purge_sessions(tmp_path, ["doomed"]).artifacts == []
    assert (artifacts / "explore-1.md").exists()


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="needs a user that file permissions apply to")
def test_an_unreadable_log_is_left_out_rather_than_failing_the_list(tmp_path):
    _session(tmp_path, "a", "one")
    locked = _session(tmp_path, "b", "two")
    locked.path.chmod(0)
    try:
        assert list(_rows(tmp_path)) == ["a"]
    finally:
        locked.path.chmod(0o600)
