"""A damaged session log costs the damaged line, never the rest of the session.

Logs are written one line at a time by a process that can be killed mid-write,
edited by hand, round-tripped through an editor that adds a BOM or CRLFs, or
left NUL-padded by a filesystem that extended the file before the data landed.
Every reader -- listing, resume, replay, the sweep -- has to step over the bad
line and keep going, and the next write has to start on a line of its own.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from quickcode.providers.base import ChatMessage
from quickcode.server.manager import Conversation
from quickcode.session.recorder import TranscriptRecorder
from quickcode.session.records import parse
from quickcode.session.store import SessionStore
from tests.test_server import FakeProvider, make_client, make_manager, ws_connect


def _path(root: Path, conv_id: str) -> Path:
    return root / ".quickcode" / "sessions" / f"{conv_id}.jsonl"


def _line(rec: dict) -> bytes:
    return (json.dumps(rec) + "\n").encode("utf-8")


def _event(seq: int, ev: dict) -> dict:
    return {"kind": "event", "seq": seq, "ts": "2026-09-01T10:00:00", "ev": ev}


def _user(text: str) -> dict:
    return {"kind": "message", "ts": "2026-09-01T10:00:00",
            "message": {"role": "user", "content": text, "tool_calls": [],
                        "tool_call_id": None, "name": None, "cache_control": False}}


def _write(root: Path, conv_id: str, data: bytes) -> Path:
    path = _path(root, conv_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


GOOD = [
    {"kind": "meta", "title": "", "model": "m"},
    _event(1, {"type": "user_message", "text": "first question"}),
    _user("first question"),
    _event(2, {"type": "assistant_message", "text": "an answer", "reasoning": "",
               "finish_reason": "stop"}),
]


def _texts(store: SessionStore) -> list[str]:
    return [e.get("text", "") for e in store.load_events()]


# ---- reading ----

def test_a_line_that_is_not_utf8_costs_that_line_and_nothing_else(tmp_path):
    bad = b'{"kind": "event", "seq": 9, "ev": {"type": "system_note", "text": "\xff\xfe"}}\n'
    _write(tmp_path, "conv", _line(GOOD[0]) + _line(GOOD[1]) + bad
           + _line(GOOD[2]) + _line(GOOD[3]))
    store = SessionStore(tmp_path, "conv")

    # The bad bytes sit inside a string, so the record itself survives with
    # replacement characters rather than being thrown away.
    assert [e["type"] for e in store.load_events()] == [
        "user_message", "system_note", "assistant_message",
    ]
    assert [m.content for m in store.load_messages()] == ["first question"]
    assert store.title() == "first question"
    assert [s.conv_id for s in SessionStore.list_sessions(tmp_path)] == ["conv"]


def test_a_byte_order_mark_does_not_cost_the_opening_meta_record(tmp_path):
    _write(tmp_path, "conv", b"\xef\xbb\xbf" + _line({"kind": "meta", "title": "Named",
                                                      "model": "m", "preset": "p1"}))
    store = SessionStore(tmp_path, "conv")
    assert store.title() == "Named"
    assert store.meta()["preset"] == "p1"
    assert SessionStore.list_sessions(tmp_path)[0].model == "m"


def test_crlf_line_endings_read_like_lf(tmp_path):
    data = b"".join(_line(r).replace(b"\n", b"\r\n") for r in GOOD)
    _write(tmp_path, "conv", data)
    store = SessionStore(tmp_path, "conv")
    assert _texts(store) == ["first question", "an answer"]
    assert len(store.load_messages()) == 1


def test_json_that_is_not_an_object_is_skipped_by_every_reader(tmp_path):
    junk = b"null\n5\n[1, 2]\n\"text\"\n"
    _write(tmp_path, "conv", _line(GOOD[0]) + junk + b"".join(_line(r) for r in GOOD[1:]))
    store = SessionStore(tmp_path, "conv")

    assert _texts(store) == ["first question", "an answer"]
    assert len(store.load_messages()) == 1
    assert store.meta()["model"] == "m"
    assert not store.is_empty()
    assert store.replay_events()
    assert SessionStore.list_sessions(tmp_path)[0].message_count == 1


def test_damaged_lines_are_counted_and_logged(tmp_path, caplog):
    _write(tmp_path, "conv", _line(GOOD[0]) + b"{not json\n" + _line(GOOD[1])
           + b'{"kind": "event", "se')
    store = SessionStore(tmp_path, "conv")
    with caplog.at_level(logging.WARNING, logger="quickcode.session"):
        assert _texts(store) == ["first question"]
    assert store.damaged_lines == [2, 4]
    assert any("conv" in r.getMessage() and "2" in r.getMessage() for r in caplog.records)


def test_an_empty_log_reads_as_an_empty_session(tmp_path):
    _write(tmp_path, "conv", b"")
    store = SessionStore(tmp_path, "conv")
    assert store.load_events() == []
    assert store.load_messages() == []
    assert store.title() == "(empty)"
    assert store.is_empty()
    assert SessionStore.empty_sessions(tmp_path) == ["conv"]


def test_a_torn_tail_is_left_unconsumed_for_the_next_read():
    # The listing index reads on from ``end``; a record still being written
    # must be read again once it is complete, not skipped for good.
    whole = _line(GOOD[0])
    parsed = parse(whole + b'{"kind": "ev')
    assert parsed.end == len(whole) and parsed.damaged == [2]
    assert parse(whole + b"\x00\x00").end == len(whole) + 2
    unterminated = whole + json.dumps(GOOD[1]).encode()
    assert parse(unterminated).end == len(unterminated)
    assert len(parse(unterminated).records) == 2


# ---- writing after damage ----

def test_the_first_record_after_a_torn_write_lands_on_a_line_of_its_own(tmp_path):
    # A crash mid-append leaves half a record with no newline. The next append
    # used to be glued onto it, so the first thing said after a crash was lost
    # along with the fragment.
    _write(tmp_path, "conv", b"".join(_line(r) for r in GOOD) + b'{"kind": "event", "seq": 3, "e')
    store = SessionStore(tmp_path, "conv")
    store.append_event({"type": "user_message", "text": "after the crash"})
    store.append_message(ChatMessage(role="user", content="after the crash"))

    reread = SessionStore(tmp_path, "conv")
    assert _texts(reread)[-1] == "after the crash"
    assert [m.content for m in reread.load_messages()][-1] == "after the crash"


def test_nul_padding_left_by_a_crash_does_not_swallow_the_next_record(tmp_path):
    _write(tmp_path, "conv", b"".join(_line(r) for r in GOOD) + b"\x00" * 64)
    store = SessionStore(tmp_path, "conv")
    store.append_event({"type": "user_message", "text": "resumed"})
    assert _texts(SessionStore(tmp_path, "conv"))[-1] == "resumed"


def test_one_write_is_one_contiguous_block(tmp_path):
    # Several records held and released together must not be split by another
    # writer; they go down in one write, in order.
    store = SessionStore(tmp_path, "conv")
    store.begin(title="", model="m")
    store.append_event({"type": "system_prompt", "text": "x" * 20_000})
    store.append_event({"type": "user_message", "text": "go"})
    lines = _path(tmp_path, "conv").read_bytes().split(b"\n")
    assert [json.loads(line)["kind"] for line in lines if line] == ["meta", "event", "event"]


# ---- sequence numbers ----

def test_sequence_numbers_stay_unique_when_a_second_writer_appends(tmp_path):
    # The replaying client dedupes by seq, so two events sharing one number
    # means one of them silently never replays. A headless `-p -c` run appending
    # to the session a window has open is the ordinary way to get two writers.
    live = SessionStore(tmp_path, "conv")
    live.append_event({"type": "user_message", "text": "one"})
    other = SessionStore(tmp_path, "conv")
    other.append_event({"type": "user_message", "text": "two"})
    other.append_event({"type": "assistant_message", "text": "three"})
    live.append_event({"type": "assistant_message", "text": "four"})

    seqs = [e["seq"] for e in SessionStore(tmp_path, "conv").load_events()]
    assert len(seqs) == len(set(seqs)) == 4
    assert seqs == sorted(seqs)


def test_reading_the_log_does_not_hide_a_second_writer_from_the_counter(tmp_path):
    live = SessionStore(tmp_path, "conv")
    live.append_event({"type": "user_message", "text": "one"})
    SessionStore(tmp_path, "conv").append_event({"type": "user_message", "text": "two"})
    # A read in between (the listing, a title lookup) must not count as the
    # store having seen what the other writer numbered.
    live.title()
    live.load_events()
    live.append_event({"type": "assistant_message", "text": "three"})

    seqs = [e["seq"] for e in SessionStore(tmp_path, "conv").load_events()]
    assert seqs == [1, 2, 3]


def test_a_held_event_is_written_in_the_same_shape_as_an_unheld_one(tmp_path):
    held = SessionStore(tmp_path, "held")
    held.begin(title="", model="m")
    TranscriptRecorder(held).emit({"type": "system_prompt", "text": "p"})
    TranscriptRecorder(held).emit({"type": "user_message", "text": "hi"})

    plain = SessionStore(tmp_path, "plain")
    TranscriptRecorder(plain).emit({"type": "system_prompt", "text": "p"})

    def first_event(conv_id):
        for raw in _path(tmp_path, conv_id).read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            if rec["kind"] == "event":
                return rec
        raise AssertionError("no event")

    assert sorted(first_event("held")["ev"]) == sorted(first_event("plain")["ev"])
    assert "seq" not in first_event("held")["ev"]


# ---- over the API ----

def test_the_session_list_survives_a_log_that_is_not_utf8(tmp_path):
    _write(tmp_path, "broken", _line(GOOD[0]) + b"\xff\xff\xff\n" + _line(GOOD[1]))
    _write(tmp_path, "fine", b"".join(_line(r) for r in GOOD))
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        assert sorted(s["conv_id"] for s in resp.json()) == ["broken", "fine"]


def test_attaching_to_a_damaged_log_replays_everything_that_is_intact(tmp_path):
    _write(tmp_path, "conv", _line(GOOD[0]) + _line(GOOD[1]) + b"\xff{garbage\n"
           + _line(GOOD[2]) + _line(GOOD[3]) + b'{"kind": "ev')
    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client, ws_connect(client, "/ws/conversation/conv") as ws:
        seen = []
        while (ev := ws.receive_json())["type"] != "replay_done":
            seen.append(ev)
        assert [e["text"] for e in seen if e["type"] in ("user_message",
                                                          "assistant_message")] == [
            "first question", "an answer",
        ]
    conv: Conversation = manager.conversations["conv"]
    assert [m.content for m in conv.agent.history.messages] == ["first question"]
