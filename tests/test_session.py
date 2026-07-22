from __future__ import annotations

import time

from quickcode.providers.base import ChatMessage
from quickcode.session.store import SessionStore, message_from_dict, message_to_dict


def _sample_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="hello there"),
        ChatMessage(
            role="assistant",
            content="calling a tool",
            tool_calls=[{"id": "call_1", "name": "read_file", "arguments": {"path": "x.py"}}],
        ),
        ChatMessage(role="tool", content="file contents", tool_call_id="call_1", name="read_file"),
        ChatMessage(role="assistant", content="done", cache_control=True),
    ]


def test_round_trip(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-a")
    store.append_meta(title="X", model="m")
    for msg in _sample_messages():
        store.append_message(msg)

    reloaded = SessionStore(tmp_path, conv_id="conv-a")
    loaded = reloaded.load_messages()

    original = _sample_messages()
    assert len(loaded) == len(original)
    for got, want in zip(loaded, original, strict=True):
        assert got.role == want.role
        assert got.content == want.content
        assert got.tool_calls == want.tool_calls
        assert got.tool_call_id == want.tool_call_id
        assert got.name == want.name


def test_store_does_not_create_dir_on_construction(tmp_path):
    SessionStore(tmp_path, conv_id="conv-lazy")
    assert not (tmp_path / ".quickcode").exists()


def test_load_messages_missing_file_returns_empty(tmp_path):
    store = SessionStore(tmp_path, conv_id="does-not-exist")
    assert store.load_messages() == []


def test_title_from_meta(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-b")
    store.append_meta(title="My Title", model="m")
    store.append_message(ChatMessage(role="user", content="irrelevant"))
    assert store.title() == "My Title"


def test_title_from_first_user_message(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-c")
    long_text = "a" * 100
    store.append_message(ChatMessage(role="user", content=long_text))
    title = store.title()
    assert title == long_text[:60]


def test_title_empty(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-empty")
    assert store.title() == "(empty)"


def test_list_sessions_newest_first(tmp_path):
    first = SessionStore(tmp_path, conv_id="conv-old")
    first.append_meta(title="Old Session", model="model-a")
    first.append_message(ChatMessage(role="user", content="hi"))

    time.sleep(0.05)

    second = SessionStore(tmp_path, conv_id="conv-new")
    second.append_meta(title="New Session", model="model-b")
    second.append_message(ChatMessage(role="user", content="hi"))
    second.append_message(ChatMessage(role="assistant", content="hello"))

    sessions = SessionStore.list_sessions(tmp_path)
    assert [s.conv_id for s in sessions] == ["conv-new", "conv-old"]

    new_info = sessions[0]
    assert new_info.title == "New Session"
    assert new_info.model == "model-b"
    assert new_info.message_count == 2

    old_info = sessions[1]
    assert old_info.title == "Old Session"
    assert old_info.model == "model-a"
    assert old_info.message_count == 1


def test_list_sessions_empty_root(tmp_path):
    assert SessionStore.list_sessions(tmp_path) == []


def test_list_sessions_skips_corrupt_file(tmp_path):
    sessions_dir = tmp_path / ".quickcode" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "corrupt.jsonl").write_text("{not json\n", encoding="utf-8")

    store = SessionStore(tmp_path, conv_id="conv-ok")
    store.append_message(ChatMessage(role="user", content="hi"))

    sessions = SessionStore.list_sessions(tmp_path)
    conv_ids = [s.conv_id for s in sessions]
    assert "conv-ok" in conv_ids
    assert "corrupt" in conv_ids  # file exists but yields defaults, not crashing
    corrupt_info = next(s for s in sessions if s.conv_id == "corrupt")
    assert corrupt_info.message_count == 0


def test_most_recent(tmp_path):
    first = SessionStore(tmp_path, conv_id="conv-1")
    first.append_message(ChatMessage(role="user", content="hi"))
    time.sleep(0.05)
    second = SessionStore(tmp_path, conv_id="conv-2")
    second.append_message(ChatMessage(role="user", content="hi"))

    assert SessionStore.most_recent(tmp_path) == "conv-2"


def test_most_recent_none_when_empty(tmp_path):
    assert SessionStore.most_recent(tmp_path) is None


def test_message_to_dict_from_dict_inverse_all_roles():
    messages = [
        ChatMessage(role="system", content="sys prompt", cache_control=True),
        ChatMessage(role="user", content="user text"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {"id": "call_1", "name": "bash", "arguments": {"cmd": "ls"}},
                {"id": "call_2", "name": "read", "arguments": {"path": "a.py"}},
            ],
        ),
        ChatMessage(role="tool", content="output", tool_call_id="call_1", name="bash"),
    ]
    for msg in messages:
        d = message_to_dict(msg)
        restored = message_from_dict(d)
        assert restored == msg
