"""Naming a session: the store's append-only rename and the route pair over it.

The interesting cases are all about a format that can only append. A title is a
``meta`` record like any other, so a session that has been renamed twice holds
two of them, and "the title" has to mean the last one.
"""

from __future__ import annotations

from quickcode.providers.base import ChatMessage
from quickcode.session.store import MAX_TITLE, SessionStore
from tests.test_projects import make_app, mkdirs
from tests.test_server import FakeProvider


def test_the_last_name_a_session_was_given_is_the_one_it_shows(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-rename")
    store.append_meta(title="", model="test/model")
    store.append_message(ChatMessage(role="user", content="derived from this"))

    assert store.title() == "derived from this"
    store.rename("first name")
    assert store.title() == "first name"
    store.rename("second name")
    assert store.title() == "second name"


def test_clearing_the_name_hands_the_session_back_to_its_derived_title(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-clear")
    store.append_message(ChatMessage(role="user", content="the first thing typed"))
    store.rename("something else")

    assert store.rename("   ") == "the first thing typed"
    assert store.title() == "the first thing typed"


def test_a_name_is_collapsed_to_one_line_and_cut_to_the_limit(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-clean")
    assert store.rename("  two\n\nlines   and  gaps ") == "two lines and gaps"
    assert len(store.rename("x" * (MAX_TITLE + 50))) == MAX_TITLE


def test_renaming_reaches_the_listing_under_both_route_shapes(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    SessionStore(root, "rootsession1").append_message(
        ChatMessage(role="user", content="in the launch directory"))
    SessionStore(alpha, "alphasession").append_message(
        ChatMessage(role="user", content="in the other project"))

    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        pid = client.post("/api/projects/open", json={"path": str(alpha)}).json()["id"]

        answer = client.patch("/api/sessions/rootsession1", json={"title": "the launcher"})
        assert answer.status_code == 200
        assert answer.json() == {"conv_id": "rootsession1", "title": "the launcher"}
        assert client.get("/api/sessions").json()[0]["title"] == "the launcher"

        scoped = client.patch(f"/api/projects/{pid}/sessions/alphasession",
                              json={"title": "the other one"})
        assert scoped.json()["title"] == "the other one"
        assert client.get(f"/api/projects/{pid}/sessions").json()[0]["title"] == "the other one"
        # Each write landed in its own project and nowhere else.
        assert client.get("/api/sessions").json()[0]["title"] == "the launcher"


def test_renaming_a_live_conversation_is_allowed_where_deleting_is_not(tmp_path):
    """The one route that mutates a live session and answers 200.

    Archiving moves the log and deleting unlinks it, both out from under the
    conversation still writing to it, so both refuse. A rename only appends —
    which is what the live writer is doing anyway — so it goes through.
    """
    hub, client = make_app(tmp_path, FakeProvider([]))
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]

        assert client.patch(f"/api/sessions/{conv_id}",
                            json={"title": "still running"}).status_code == 200
        assert client.delete(f"/api/sessions/{conv_id}").status_code == 409
        assert client.post(f"/api/sessions/{conv_id}/archive").status_code == 409

        listed = client.get("/api/sessions").json()[0]
        assert listed["live"] is True
        assert listed["title"] == "still running"


def test_a_rename_survives_resuming_the_session(tmp_path):
    hub, client = make_app(tmp_path, FakeProvider([]))
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        client.patch(f"/api/sessions/{conv_id}", json={"title": "kept"})
        # Resuming re-opens the same log; it must not write a blank title over
        # the one the user chose.
        client.post("/api/conversations", json={"resume": conv_id})
        assert client.get("/api/sessions").json()[0]["title"] == "kept"


def test_renaming_refuses_what_it_cannot_name(tmp_path):
    hub, client = make_app(tmp_path, FakeProvider([]))
    with client:
        SessionStore(tmp_path, "realsession1").append_meta(title="", model="test/model")

        assert client.patch("/api/sessions/nosuchsession", json={"title": "x"}).status_code == 404
        assert client.patch("/api/sessions/bad!id", json={"title": "x"}).status_code == 404
        assert client.patch("/api/sessions/realsession1", json={}).status_code == 400
        assert client.patch("/api/sessions/realsession1", json={"title": 7}).status_code == 400
        too_long = {"title": "y" * (MAX_TITLE + 1)}
        assert client.patch("/api/sessions/realsession1", json=too_long).status_code == 400
