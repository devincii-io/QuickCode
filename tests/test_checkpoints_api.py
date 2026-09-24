"""The checkpoint routes, against a real app, a real conversation and real files.

The conversation is driven over its WebSocket like the UI drives it: the model
(scripted) reads and edits a file, and the routes are asked what that turn
changed, what a rewind would do, and then to do it.
"""

from __future__ import annotations

import json
from pathlib import Path

from quickcode.core.events import TextDelta, ToolCallEnd, ToolCallStart, TurnDone
from quickcode.server.projects import project_id
from quickcode.session.store import SessionStore
from tests.test_checkpoints import Session, edit, read
from tests.test_server import FakeProvider, make_client, make_manager, recv_until, ws_connect


def tool_round(cid: str, name: str, **args) -> list:
    return [ToolCallStart(cid, name), ToolCallEnd(cid, name, json.dumps(args)),
            TurnDone("tool_calls")]


def edited_conversation(tmp_path: Path, client, path: Path) -> str:
    """A live conversation whose one turn read ``path`` and edited it."""
    conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
    with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
        recv_until(ws, "replay_done")
        ws.send_text(json.dumps({"type": "user_message", "text": "fix it"}))
        assert recv_until(ws, "checkpoint")["path"] == path.name
        recv_until(ws, "assistant_message")
    return conv_id


def app_with_edit(tmp_path: Path):
    target = tmp_path / "main.py"
    target.write_bytes(b"print('before')\r\n")
    provider = FakeProvider([
        tool_round("c1", "read", file_path=str(target)),
        tool_round("c2", "edit", file_path=str(target), old_string="before",
                   new_string="after"),
        [TextDelta("done"), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    manager.default_mode = "auto-edit"
    return manager, make_client(manager), target


def test_list_preview_and_rewind_a_live_conversation(tmp_path):
    manager, client, target = app_with_edit(tmp_path)
    with client:
        conv_id = edited_conversation(tmp_path, client, target)
        assert target.read_bytes() == b"print('after')\r\n"
        base = f"/api/sessions/{conv_id}/checkpoints"

        listing = client.get(base).json()
        [cp] = listing["checkpoints"]
        [entry] = cp["files"]
        assert cp["turn"] == 1
        assert {k: entry[k] for k in ("path", "change", "added", "removed", "restorable",
                                      "tool")} == {
            "path": "main.py", "change": "modified", "added": 1, "removed": 1,
            "restorable": True, "tool": "edit"}
        assert entry["calls"] == ["c2"]
        assert "bash" in listing["untracked"]

        preview = client.post(f"{base}/preview", json={"turn": 1}).json()
        [row] = preview["files"]
        assert (row["action"], row["conflicts"]) == ("restore", [])
        assert "-print('after')" in row["diff"] and "+print('before')" in row["diff"]
        assert target.read_bytes() == b"print('after')\r\n", "a preview writes nothing"

        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            done = client.post(f"{base}/rewind", json={"turn": 1}).json()
            heard = recv_until(ws, "files_rewound")
        assert target.read_bytes() == b"print('before')\r\n", "CRLF and all"
        assert done["files"] == [{"path": "main.py", "action": "restored", "from_turn": 1,
                                  "backup": True}]
        assert heard["rewind_id"] == done["rewind_id"]
        assert heard["files"] == [{"path": "main.py", "action": "restored", "from_turn": 1}]

    logged = [e for e in SessionStore(tmp_path, conv_id).load_events()
              if e["type"] == "files_rewound"]
    assert [e["to_turn"] for e in logged] == [1]


def test_the_project_shape_answers_the_same(tmp_path):
    manager, client, target = app_with_edit(tmp_path)
    with client:
        conv_id = edited_conversation(tmp_path, client, target)
        pid = project_id(tmp_path)
        scoped = client.get(f"/api/projects/{pid}/sessions/{conv_id}/checkpoints")
        assert scoped.status_code == 200
        assert scoped.json() == client.get(f"/api/sessions/{conv_id}/checkpoints").json()
        done = client.post(f"/api/projects/{pid}/sessions/{conv_id}/checkpoints/rewind",
                           json={"turn": 1, "paths": ["main.py"]})
        assert done.status_code == 200
        assert target.read_bytes() == b"print('before')\r\n"


def test_a_conflict_is_refused_until_forced(tmp_path):
    manager, client, target = app_with_edit(tmp_path)
    with client:
        conv_id = edited_conversation(tmp_path, client, target)
        target.write_bytes(b"typed by hand\n")
        base = f"/api/sessions/{conv_id}/checkpoints"

        refused = client.post(f"{base}/rewind", json={"turn": 1})
        assert refused.status_code == 409
        [conflict] = refused.json()["detail"]["conflicts"]
        assert conflict["path"] == "main.py"
        assert conflict["conflicts"][0]["kind"] == "modified"
        assert target.read_bytes() == b"typed by hand\n"

        forced = client.post(f"{base}/rewind", json={"turn": 1, "force": True})
        assert forced.status_code == 200
        assert target.read_bytes() == b"print('before')\r\n"


def test_a_busy_conversation_is_not_rewound_under_itself(tmp_path):
    manager, client, target = app_with_edit(tmp_path)
    with client:
        conv_id = edited_conversation(tmp_path, client, target)
        manager.get(conv_id).agent.busy = True
        try:
            answer = client.post(f"/api/sessions/{conv_id}/checkpoints/rewind",
                                 json={"turn": 1})
        finally:
            manager.get(conv_id).agent.busy = False
        assert answer.status_code == 409
        assert "turn is running" in answer.json()["detail"]
        assert target.read_bytes() == b"print('after')\r\n"


def test_a_conversation_nobody_has_open_is_rewound_and_logged_on_disk(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("was\n", encoding="utf-8")
    headless = Session(tmp_path, conv_id="headless1")
    headless.turn([read(f)], [edit(f, "was", "is")])

    manager = make_manager(tmp_path, FakeProvider([]))
    with make_client(manager) as client:
        answer = client.post("/api/sessions/headless1/checkpoints/rewind", json={"turn": 1})
        assert answer.status_code == 200
    assert f.read_text(encoding="utf-8") == "was\n"
    [logged] = [e for e in headless.store.load_events() if e["type"] == "files_rewound"]
    assert logged["turn"] == 1 and logged["files"][0]["path"] == "a.txt"


def test_bad_requests_are_named(tmp_path):
    manager, client, target = app_with_edit(tmp_path)
    with client:
        assert client.get("/api/sessions/nosuchconv/checkpoints").status_code == 404
        assert client.get("/api/sessions/..%2F..%2Fetc/checkpoints").status_code == 404
        conv_id = edited_conversation(tmp_path, client, target)
        base = f"/api/sessions/{conv_id}/checkpoints"
        for body in ({}, {"turn": 0}, {"turn": True}, {"turn": "1"},
                     {"turn": 1, "paths": []}, {"turn": 1, "paths": [3]},
                     {"turn": 1, "force": "yes"}, [1]):
            answer = client.post(f"{base}/preview", json=body)
            assert answer.status_code == 400, body
        unknown = client.post(f"{base}/rewind", json={"turn": 1, "paths": ["../../etc/passwd"]})
        assert unknown.status_code == 400
        assert unknown.json()["detail"]["paths"] == ["../../etc/passwd"]
        assert target.read_bytes() == b"print('after')\r\n"
        # A turn after every checkpoint has nothing to rewind.
        later = client.post(f"{base}/rewind", json={"turn": 9}).json()
        assert (later["rewind_id"], later["files"]) == (None, [])
