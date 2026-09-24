"""Searching a project's sessions: what is matched, what comes back, and the
three limits that keep a search through years of logs from stalling a request.
"""

from __future__ import annotations

import itertools
import os

import pytest
from starlette.testclient import TestClient

from quickcode.providers.base import ChatMessage
from quickcode.server import auth
from quickcode.server.app import create_app
from quickcode.session.search import Limits, QueryError, parse_query, search_sessions, snippet
from quickcode.session.store import SessionStore
from tests.test_projects import make_app, mkdirs
from tests.test_server import FakeProvider, make_manager


def session(root, conv_id, *, title="", said=(), answered=(), tools=(), mtime=None):
    store = SessionStore(root, conv_id)
    store.append_meta(title=title, model="test/model")
    for text in said:
        store.append_event({"type": "user_message", "text": text, "turn": 1})
    for name in tools:
        store.append_event({"type": "tool_call", "id": name, "name": name, "arguments": "{}"})
        store.append_event({"type": "tool_result", "id": name, "name": name,
                            "content": "the result text is never searched", "is_error": False})
    for text in answered:
        store.append_event({"type": "assistant_message", "text": text, "finish_reason": "stop"})
    if mtime is not None:
        os.utime(store.path, (mtime, mtime))
    return store


def ids(answer):
    return [r["conv_id"] for r in answer["results"]]


def test_terms_are_case_insensitive_and_must_all_occur_in_one_message(tmp_path):
    session(tmp_path, "both", said=["The LOGIN page redirects twice"])
    session(tmp_path, "split", said=["the login page"], answered=["it redirects"])

    answer = search_sessions(tmp_path, "login Redirects")

    assert ids(answer) == ["both"]
    [hit] = answer["results"][0]["hits"]
    assert hit["where"] == "user"
    assert hit["snippet"] == "The LOGIN page redirects twice"
    assert isinstance(hit["seq"], int)


def test_a_quoted_phrase_is_one_term(tmp_path):
    session(tmp_path, "phrase", said=["open the settings page now"])
    session(tmp_path, "scattered", said=["the page with settings"])

    assert ids(search_sessions(tmp_path, '"settings page"')) == ["phrase"]
    assert parse_query('  "Settings   Page"  now "') == ["settings page", "now"]


def test_titles_answers_and_tool_names_are_searched_but_tool_output_is_not(tmp_path):
    session(tmp_path, "titled", title="Flaky upload test", said=["hello"])
    session(tmp_path, "answered", answered=["The upload handler retries"])
    session(tmp_path, "tooled", tools=["web_fetch"])

    upload = search_sessions(tmp_path, "upload")
    assert set(ids(upload)) == {"titled", "answered"}
    titled = next(r for r in upload["results"] if r["conv_id"] == "titled")
    assert titled["title_match"] and titled["hits"] == []
    answered = next(r for r in upload["results"] if r["conv_id"] == "answered")
    assert answered["hits"][0]["where"] == "assistant"

    tool = search_sessions(tmp_path, "web_fetch")
    assert ids(tool) == ["tooled"]
    assert tool["results"][0]["hits"][0] == {**tool["results"][0]["hits"][0], "where": "tool",
                                             "snippet": "web_fetch"}
    assert search_sessions(tmp_path, "never searched")["results"] == []


def test_each_hit_names_the_event_it_came_from(tmp_path):
    store = session(tmp_path, "seqs", said=["first question", "second question about caching"])
    events = store.load_events()
    wanted = next(e["seq"] for e in events if "caching" in e.get("text", ""))

    [result] = search_sessions(tmp_path, "caching")["results"]
    assert result["hits"][0]["seq"] == wanted
    assert result["hit_count"] == 1


def test_sessions_come_newest_first_with_a_capped_number_of_snippets(tmp_path):
    session(tmp_path, "old", said=["deploy"] * 5, mtime=1_000_000)
    session(tmp_path, "new", said=["deploy again"], mtime=2_000_000)

    answer = search_sessions(tmp_path, "deploy", limits=Limits(max_hits=2))
    assert ids(answer) == ["new", "old"]
    old = answer["results"][1]
    assert len(old["hits"]) == 2 and old["hit_count"] == 5


def test_the_result_limit_stops_the_scan_and_says_so(tmp_path):
    for i in range(4):
        session(tmp_path, f"s{i}", said=["needle"], mtime=1_000_000 + i)

    answer = search_sessions(tmp_path, "needle", limits=Limits(max_sessions=2))
    assert ids(answer) == ["s3", "s2"]
    assert answer["stopped"] == "results"
    assert answer["sessions"] == 4


def test_the_byte_budget_stops_the_scan_and_says_so(tmp_path):
    for i in range(3):
        # The needle past the first 60 characters, so it is not in the title too.
        session(tmp_path, f"s{i}", said=["x" * 2000 + " needle"], mtime=1_000_000 + i)
    one_log = (tmp_path / ".quickcode" / "sessions" / "s0.jsonl").stat().st_size

    answer = search_sessions(tmp_path, "needle", limits=Limits(max_bytes=one_log + 100))
    assert answer["stopped"] == "bytes"
    assert answer["bytes"] <= one_log + 100
    assert ids(answer) == ["s2"]


def test_the_time_budget_stops_the_scan_and_says_so(tmp_path):
    for i in range(3):
        session(tmp_path, f"s{i}", said=["needle"], mtime=1_000_000 + i)
    ticks = itertools.count(0, 1.0)

    answer = search_sessions(tmp_path, "needle", limits=Limits(seconds=2.5),
                             clock=lambda: next(ticks))
    assert answer["stopped"] == "time"
    assert 0 < len(answer["results"]) < 3


def test_an_overlong_line_is_skipped_but_still_counted(tmp_path, monkeypatch):
    monkeypatch.setattr("quickcode.session.search.LINE_CAP", 256)
    store = session(tmp_path, "long", said=["needle " + "y" * 1000, "short needle after it"])

    answer = search_sessions(tmp_path, "needle")
    [result] = answer["results"]
    assert [h["snippet"] for h in result["hits"]] == ["short needle after it"]
    assert answer["bytes"] == store.path.stat().st_size


def test_archived_sessions_are_searched_only_when_asked_for(tmp_path):
    session(tmp_path, "filed", said=["archived needle"]).archive()

    assert search_sessions(tmp_path, "needle")["results"] == []
    [hit] = search_sessions(tmp_path, "needle", include_archived=True)["results"]
    assert hit["archived"] is True


def test_a_log_from_before_the_event_log_is_searched_through_its_messages(tmp_path):
    store = SessionStore(tmp_path, "legacy")
    store.append_message(ChatMessage(
        role="user",
        content="why is the cache cold, when it was warm a minute ago and nothing changed?"
                "\n<system-reminder>hidden needle</system-reminder>"))
    store.append_message(ChatMessage(role="assistant", content="the cache was evicted",
                                     tool_calls=[{"id": "1", "name": "grep", "arguments": "{}"}]))

    [result] = search_sessions(tmp_path, "cache")["results"]
    assert [h["where"] for h in result["hits"]] == ["user", "assistant"]
    assert all(h["seq"] is None for h in result["hits"])
    assert ids(search_sessions(tmp_path, "grep")) == ["legacy"]
    assert search_sessions(tmp_path, "hidden needle")["results"] == []


def test_a_log_with_events_does_not_also_answer_from_its_messages(tmp_path):
    store = session(tmp_path, "both", said=["the question"])
    store.append_message(ChatMessage(role="user", content="the question"))

    [result] = search_sessions(tmp_path, "question")["results"]
    assert result["hit_count"] == 1 and result["hits"][0]["seq"] is not None


def test_damaged_lines_and_non_ascii_terms_are_handled(tmp_path):
    store = session(tmp_path, "mixed", said=["Überprüfe den Café-Code"])
    with store.path.open("ab") as f:
        f.write(b'{"kind": "event", "ev": {"type": "user_message", "text": "torn needle\n')
        f.write(b"\x00\x00garbage\n")

    assert ids(search_sessions(tmp_path, "café")) == ["mixed"]
    assert ids(search_sessions(tmp_path, "ÜBERPRÜFE")) == ["mixed"]
    assert search_sessions(tmp_path, "torn needle")["results"] == []


def test_a_log_an_editor_saved_with_a_bom_is_searched_from_its_first_line(tmp_path):
    logs = tmp_path / ".quickcode" / "sessions"
    logs.mkdir(parents=True)
    (logs / "bom.jsonl").write_bytes(
        b'\xef\xbb\xbf{"kind": "event", "seq": 1, "ev": {"type": "user_message", "text": "first needle"}}\r\n')

    [result] = search_sessions(tmp_path, "needle")["results"]
    assert result["hits"][0]["seq"] == 1


def test_a_quote_or_backslash_in_a_term_is_matched_in_the_decoded_text(tmp_path):
    session(tmp_path, "escaped", said=['run C:\\tools\\build.cmd --flag "x"'])

    assert ids(search_sessions(tmp_path, "C:\\tools\\build")) == ["escaped"]


@pytest.mark.parametrize("query", ["", "   ", "a", '""', "x" * 201, " ".join("abcdefghi")])
def test_queries_that_cannot_be_searched_are_refused(query):
    with pytest.raises(QueryError):
        parse_query(query)


def test_a_snippet_is_one_line_centred_near_the_match():
    text = "start " + "a" * 300 + "\n\nthe NEEDLE is here\n" + "b" * 300
    out = snippet(text, "needle", width=60)
    assert "NEEDLE" in out and "\n" not in out
    assert out.startswith("…") and out.endswith("…")
    assert snippet("short needle", "needle") == "short needle"


def test_the_route_answers_in_both_shapes_and_refuses_a_bad_query(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    session(root, "rootsession", said=["launch directory needle"])
    session(alpha, "alphasession", said=["other project needle"])

    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        pid = client.post("/api/projects/open", json={"path": str(alpha)}).json()["id"]

        here = client.get("/api/sessions/search", params={"q": "needle"})
        assert here.status_code == 200
        assert ids(here.json()) == ["rootsession"]

        there = client.get(f"/api/projects/{pid}/sessions/search", params={"q": "NEEDLE"})
        assert ids(there.json()) == ["alphasession"]
        assert there.json()["results"][0]["hits"][0]["snippet"] == "other project needle"

        refused = client.get("/api/sessions/search", params={"q": "x"})
        assert refused.status_code == 400
        assert "two characters" in refused.json()["detail"]

        capped = client.get("/api/sessions/search", params={"q": "needle", "limit": 0})
        assert capped.status_code == 200 and len(capped.json()["results"]) == 1


def test_the_route_is_behind_the_token_like_every_other(tmp_path):
    session(tmp_path, "private", said=["what I typed"])
    token = "t" * 43
    app = create_app(make_manager(tmp_path, FakeProvider([])), port=8642, token=token)
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        params = {"q": "typed"}
        assert client.get("/api/sessions/search", params=params).status_code == 403
        answer = client.get("/api/sessions/search", params=params, headers={auth.HEADER: token})
        assert ids(answer.json()) == ["private"]
