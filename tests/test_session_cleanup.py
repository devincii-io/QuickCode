"""Session management: archival, complete deletion, and the empty sweep.

The dangerous case gets its own coverage on both levels: a session whose
message log is empty but whose *event* log holds the transcript (a turn
interrupted before persistence) must never be mistaken for an abandoned one.
"""

from __future__ import annotations

import json
from pathlib import Path

from quickcode.providers.base import ChatMessage
from quickcode.session.store import SessionStore, purge_sessions
from tests.test_server import (
    FakeProvider,
    make_client,
    make_manager,
    recv_until,
    ws_connect,
)


def write_session(root: Path, conv_id: str, records: list[dict]) -> Path:
    path = root / ".quickcode" / "sessions" / f"{conv_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def meta(**fields):
    return {"kind": "meta", **fields}


def message(role: str, content: str):
    return {
        "kind": "message",
        "ts": "2026-08-17T09:00:00",
        "message": {"role": role, "content": content, "tool_calls": [],
                    "tool_call_id": None, "name": None, "cache_control": False},
    }


def event(seq: int, ev: dict):
    return {"kind": "event", "seq": seq, "ts": "2026-08-17T09:00:00", "ev": ev}


EVENT_ONLY = [
    meta(model="m"),
    event(1, {"type": "user_message", "text": "interrupted before persistence"}),
    event(2, {"type": "assistant_message", "text": "half a turn", "reasoning": "",
              "finish_reason": "stop"}),
]


# ---- store level ----

def test_archive_round_trip_moves_the_file_and_keeps_the_bytes(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-a")
    store.append_meta(title="Keep me", model="m")
    store.append_message(ChatMessage(role="user", content="hello"))
    original = store.path.read_bytes()

    assert store.archive() is True
    assert store.archived is True
    assert not (tmp_path / ".quickcode" / "sessions" / "conv-a.jsonl").exists()
    assert (tmp_path / ".quickcode" / "sessions" / "archive" / "conv-a.jsonl").exists()
    assert store.path.read_bytes() == original

    assert [s.conv_id for s in SessionStore.list_sessions(tmp_path)] == []
    listed = SessionStore.list_sessions(tmp_path, include_archived=True)
    assert [(s.conv_id, s.archived, s.title) for s in listed] == [("conv-a", True, "Keep me")]
    assert [s.conv_id for s in SessionStore.list_sessions(tmp_path, archived_only=True)] == ["conv-a"]

    assert store.archive() is False       # already archived
    assert store.unarchive() is True
    assert store.archived is False
    assert store.path.read_bytes() == original
    assert [s.conv_id for s in SessionStore.list_sessions(tmp_path)] == ["conv-a"]
    assert store.unarchive() is False


def test_archived_session_is_invisible_to_a_plain_glob(tmp_path):
    """The listing an older build does is non-recursive, so the archive is
    simply not there for it — no unknown record for it to misread."""
    SessionStore(tmp_path, conv_id="conv-a").append_meta(title="x")
    SessionStore(tmp_path, conv_id="conv-a").archive()
    sessions_dir = tmp_path / ".quickcode" / "sessions"
    assert list(sessions_dir.glob("*.jsonl")) == []


def test_archived_session_still_loads_and_keeps_appending(tmp_path):
    store = SessionStore(tmp_path, conv_id="conv-a")
    store.append_message(ChatMessage(role="user", content="first"))
    store.archive()

    reopened = SessionStore(tmp_path, conv_id="conv-a")
    assert [m.content for m in reopened.load_messages()] == ["first"]
    reopened.append_message(ChatMessage(role="assistant", content="second"))
    # Appending must not resurrect a second log under the active name.
    assert not (tmp_path / ".quickcode" / "sessions" / "conv-a.jsonl").exists()
    assert [m.content for m in SessionStore(tmp_path, "conv-a").load_messages()] == [
        "first", "second"
    ]


def test_event_only_session_is_not_empty(tmp_path):
    write_session(tmp_path, "conv-live", EVENT_ONLY)
    store = SessionStore(tmp_path, "conv-live")
    assert store.load_messages() == []          # the trap: no message records…
    assert store.is_empty() is False            # …but a real transcript
    assert store.title() == "interrupted before persistence"
    assert SessionStore.empty_sessions(tmp_path) == []


def test_empty_sessions_finds_only_the_abandoned_ones(tmp_path):
    write_session(tmp_path, "abandoned", [meta(model="m")])
    write_session(tmp_path, "nothing", [])
    write_session(tmp_path, "real", [meta(model="m"), message("user", "hi")])
    write_session(tmp_path, "interrupted", EVENT_ONLY)
    # A permission event alone is not a transcript; a session that only asked
    # and never spoke is still abandoned.
    write_session(tmp_path, "asked-only", [
        meta(model="m"), event(1, {"type": "permission_request", "tool": "bash"}),
    ])
    assert sorted(SessionStore.empty_sessions(tmp_path)) == [
        "abandoned", "asked-only", "nothing",
    ]


def test_purge_removes_board_and_owned_artifacts(tmp_path):
    write_session(tmp_path, "conv-a", [
        meta(model="m"),
        message("tool", "full report (9000 chars) written to "
                        r"C:\demo\.quickcode\artifacts\explore-1.md; read that file"),
    ])
    write_session(tmp_path, "conv-b", [meta(model="m"), message("user", "unrelated")])
    board_a = tmp_path / ".quickcode" / "tasks" / "conv-a"
    board_b = tmp_path / ".quickcode" / "tasks" / "conv-b"
    for board in (board_a, board_b):
        board.mkdir(parents=True)
        (board / "board.json").write_text("{}", encoding="utf-8")
    artifacts = tmp_path / ".quickcode" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "explore-1.md").write_text("report", encoding="utf-8")
    (artifacts / "explore-2.md").write_text("someone else's", encoding="utf-8")

    result = purge_sessions(tmp_path, ["conv-a"])

    assert result.sessions == ["conv-a"]
    assert result.boards == ["conv-a"]
    assert result.artifacts == ["explore-1.md"]
    assert not (tmp_path / ".quickcode" / "sessions" / "conv-a.jsonl").exists()
    assert not board_a.exists()
    assert board_b.exists()
    assert not (artifacts / "explore-1.md").exists()
    assert (artifacts / "explore-2.md").exists()


def test_purge_keeps_an_artifact_a_survivor_still_references(tmp_path):
    """Artifact ids restart per conversation, so two sessions can name the
    same file. Deleting one must not pull the file out from under the other."""
    marker = "written to /p/.quickcode/artifacts/explore-1.md;"
    write_session(tmp_path, "conv-a", [meta(model="m"), message("tool", marker)])
    write_session(tmp_path, "conv-b", [meta(model="m"), message("tool", marker)])
    artifacts = tmp_path / ".quickcode" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "explore-1.md").write_text("shared name", encoding="utf-8")

    result = purge_sessions(tmp_path, ["conv-a"])
    assert result.artifacts == []
    assert (artifacts / "explore-1.md").exists()

    purge_sessions(tmp_path, ["conv-b"])
    assert not (artifacts / "explore-1.md").exists()


def test_purge_deletes_an_archived_session_too(tmp_path):
    write_session(tmp_path, "conv-a", [meta(model="m"), message("user", "hi")])
    SessionStore(tmp_path, "conv-a").archive()
    assert purge_sessions(tmp_path, ["conv-a"]).sessions == ["conv-a"]
    assert SessionStore.list_sessions(tmp_path, include_archived=True) == []


# ---- API level ----

def make_project(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    return manager, make_client(manager)


def test_api_archive_and_unarchive(tmp_path):
    write_session(tmp_path, "conv-a", [meta(title="Kept", model="m"), message("user", "hi")])
    manager, client = make_project(tmp_path)
    with client:
        assert client.post("/api/sessions/conv-a/archive").json()["archived"] is True
        assert client.get("/api/sessions").json() == []
        listed = client.get("/api/sessions?archived=true").json()
        assert [(s["conv_id"], s["archived"]) for s in listed] == [("conv-a", True)]

        assert client.post("/api/sessions/conv-a/unarchive").json()["archived"] is False
        assert [s["conv_id"] for s in client.get("/api/sessions").json()] == ["conv-a"]
        assert client.post("/api/sessions/nope/archive").status_code == 404


def test_api_project_route_shape_matches(tmp_path):
    write_session(tmp_path, "conv-a", [meta(model="m"), message("user", "hi")])
    manager, client = make_project(tmp_path)
    with client:
        pid = client.get("/api/projects").json()["projects"][0]["id"]
        assert client.post(f"/api/projects/{pid}/sessions/conv-a/archive").status_code == 200
        assert client.get(f"/api/projects/{pid}/sessions").json() == []
        assert client.get(f"/api/projects/{pid}/sessions?archived=true").json()[0]["archived"]
        assert client.post(f"/api/projects/{pid}/sessions/conv-a/unarchive").status_code == 200
        assert client.delete(f"/api/projects/{pid}/sessions/conv-a").status_code == 204


def test_api_refuses_to_touch_a_live_session(tmp_path):
    write_session(tmp_path, "conv-a", [meta(model="m"), message("user", "hi")])
    manager, client = make_project(tmp_path)
    with client, ws_connect(client, "/ws/conversation/conv-a") as ws:
        recv_until(ws, "replay_done")
        assert "conv-a" in manager.conversations
        assert client.delete("/api/sessions/conv-a").status_code == 409
        assert client.post("/api/sessions/conv-a/archive").status_code == 409
        # Bulk skips it instead of failing the whole request.
        body = client.post("/api/sessions/delete", json={"conv_ids": ["conv-a"]}).json()
        assert body["deleted"] == []
        assert body["skipped"] == [{"conv_id": "conv-a", "reason": "live"}]
        assert (tmp_path / ".quickcode" / "sessions" / "conv-a.jsonl").exists()


def test_api_cleanup_spares_the_event_only_session(tmp_path):
    write_session(tmp_path, "abandoned", [meta(model="m")])
    write_session(tmp_path, "interrupted", EVENT_ONLY)
    write_session(tmp_path, "real", [meta(model="m"), message("user", "hi")])
    manager, client = make_project(tmp_path)
    with client:
        assert client.post("/api/sessions/cleanup", json={"dry_run": True}).json() == {
            "candidates": ["abandoned"], "deleted": [], "skipped": [],
        }
        assert (tmp_path / ".quickcode" / "sessions" / "abandoned.jsonl").exists()

        body = client.post("/api/sessions/cleanup", json={}).json()
        assert body["deleted"] == ["abandoned"]
        assert sorted(s["conv_id"] for s in client.get("/api/sessions").json()) == [
            "interrupted", "real",
        ]


def test_api_cleanup_leaves_the_archive_alone(tmp_path):
    write_session(tmp_path, "archived-empty", [meta(model="m")])
    manager, client = make_project(tmp_path)
    with client:
        client.post("/api/sessions/archived-empty/archive")
        assert client.post("/api/sessions/cleanup", json={}).json()["deleted"] == []
        assert client.get("/api/sessions?archived=true").json()[0]["conv_id"] == "archived-empty"


def test_api_bulk_delete_cleans_siblings(tmp_path):
    write_session(tmp_path, "conv-a", [
        meta(model="m"),
        message("tool", "written to /p/.quickcode/artifacts/explore-1.md; rest"),
    ])
    write_session(tmp_path, "conv-b", [meta(model="m"), message("user", "hi")])
    board = tmp_path / ".quickcode" / "tasks" / "conv-a"
    board.mkdir(parents=True)
    (board / "board.json").write_text("{}", encoding="utf-8")
    artifacts = tmp_path / ".quickcode" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "explore-1.md").write_text("report", encoding="utf-8")

    manager, client = make_project(tmp_path)
    with client:
        body = client.post(
            "/api/sessions/delete", json={"conv_ids": ["conv-a", "conv-b", "ghost"]}
        ).json()
        assert sorted(body["deleted"]) == ["conv-a", "conv-b"]
        assert body["boards"] == ["conv-a"]
        assert body["artifacts"] == ["explore-1.md"]
        assert body["skipped"] == [{"conv_id": "ghost", "reason": "missing"}]
        assert client.get("/api/sessions").json() == []
    assert not board.exists()
    assert not (artifacts / "explore-1.md").exists()


def test_api_bulk_delete_rejects_a_bad_body(tmp_path):
    manager, client = make_project(tmp_path)
    with client:
        assert client.post("/api/sessions/delete", json={}).status_code == 400
        assert client.post("/api/sessions/delete", json={"conv_ids": []}).status_code == 400
        # A traversal attempt is a bad id, not a path.
        assert client.post(
            "/api/sessions/delete", json={"conv_ids": ["../../etc/passwd"]}
        ).status_code == 400


def test_api_resuming_an_archived_session_restores_it(tmp_path):
    write_session(tmp_path, "conv-a", [meta(model="m"), message("user", "hi")])
    manager, client = make_project(tmp_path)
    with client:
        client.post("/api/sessions/conv-a/archive")
        assert client.post("/api/conversations", json={"resume": "conv-a"}).json() == {
            "conv_id": "conv-a"
        }
        listed = client.get("/api/sessions").json()
        assert [(s["conv_id"], s["archived"]) for s in listed] == [("conv-a", False)]
