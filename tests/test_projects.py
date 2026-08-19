"""Multi-project hosting: id derivation, the persisted registry, the
project-scoped API surface, and the directory browser."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quickcode.config import Config
from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.server.app import create_app
from quickcode.server.projects import ProjectHub, ProjectRegistry, list_dirs, project_id
from quickcode.session.store import SessionStore
from tests.conftest import REAL_CONFIG_DIR
from tests.test_server import FakeProvider, recv_until, ws_connect


async def _no_mcp(cwd):
    """Never spawn real MCP servers from a test run."""
    return [], []


def make_hub(tmp_path: Path, provider, default_dir: Path) -> ProjectHub:
    cfg = Config()
    cfg.last_model = "test/model"
    hub = ProjectHub(
        config=cfg,
        provider=provider,
        registry=ProjectRegistry(tmp_path / "projects.json"),
        mcp_connect=_no_mcp,
    )
    # Opened outside the app's loop, exactly as the launcher does at startup.
    asyncio.run(hub.open(default_dir, make_default=True))
    return hub


def make_app(tmp_path: Path, provider, default_dir: Path | None = None):
    hub = make_hub(tmp_path, provider, default_dir or tmp_path)
    app = create_app(hub, host="127.0.0.1", port=8642, token="")
    return hub, TestClient(app, base_url="http://127.0.0.1:8642")


def mkdirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


# ---- ids ----


def test_project_id_is_stable_and_normalized(tmp_path):
    proj = tmp_path / "Proj"
    proj.mkdir()
    first = project_id(proj)
    assert len(first) == 12
    assert first == project_id(str(proj))
    assert first == project_id(proj / "sub" / "..")
    assert first == project_id(str(proj).replace("\\", "/"))
    if os.name == "nt":
        assert first == project_id(str(proj).upper())


# ---- registry ----


def test_registry_persists_and_prunes_vanished_dirs(tmp_path):
    reg_path = tmp_path / "projects.json"
    a, b = tmp_path / "a", tmp_path / "b"
    mkdirs(a, b)

    reg = ProjectRegistry(reg_path)
    reg.touch(a)
    reg.touch(b)
    assert reg_path.exists()

    reloaded = ProjectRegistry(reg_path)
    assert {e.id for e in reloaded.list()} == {project_id(a), project_id(b)}
    assert {e.name for e in reloaded.list()} == {"a", "b"}
    assert all(e.last_opened for e in reloaded.list())

    shutil.rmtree(b)
    assert {e.id for e in reloaded.list()} == {project_id(a)}
    # The record survives on disk: a project on an unmounted drive comes back.
    assert len(json.loads(reg_path.read_text(encoding="utf-8"))["projects"]) == 2


def test_registry_tolerates_a_corrupt_file(tmp_path):
    reg_path = tmp_path / "projects.json"
    reg_path.write_text("{not json", encoding="utf-8")
    assert ProjectRegistry(reg_path).list() == []


def test_ephemeral_registry_writes_nothing(tmp_path):
    reg = ProjectRegistry.ephemeral()
    reg.touch(tmp_path)
    assert [e.id for e in reg.list()] == [project_id(tmp_path)]
    assert not (Path.home() / ".quickcode" / "projects.json.tmp").exists()


def _real_projects_json() -> tuple[int, int] | None:
    p = REAL_CONFIG_DIR / "projects.json"
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def test_a_hub_built_without_a_registry_does_not_write_to_the_developers_home(tmp_path):
    """The one that got away: a hub with no `registry=` still persists, but into
    the sandbox, never into the machine running the tests.

    This is the exact shape that filled a home screen with 258 pytest temp
    directories -- `ProjectHub(...)` with no registry, then `open()`. It is
    pinned here rather than left to the session-wide snapshot in conftest
    because that snapshot names the symptom at the end of a 30-second run,
    while this names the cause in the file that owns it.
    """
    before = _real_projects_json()
    cfg = Config()
    cfg.last_model = "test/model"
    hub = ProjectHub(config=cfg, provider=FakeProvider([]), mcp_connect=_no_mcp)
    try:
        asyncio.run(hub.open(tmp_path, make_default=True))
        written = Path(hub.registry.path)
        assert REAL_CONFIG_DIR != written.parent and REAL_CONFIG_DIR not in written.parents
        # It really did register the project -- the leak was a real write to a
        # real file, so a test that passes because nothing happened proves less.
        assert written.exists()
        assert [e.id for e in hub.registry.list()] == [project_id(tmp_path)]
    finally:
        asyncio.run(hub.close())
    assert _real_projects_json() == before


# ---- /api/projects ----


def test_projects_open_and_list(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        info = client.post("/api/projects/open", json={"path": str(alpha)}).json()
        assert info == {"id": project_id(alpha), "path": str(alpha.resolve()), "name": "alpha"}

        listing = client.get("/api/projects").json()
        assert listing["home"] == str(Path.home())
        by_id = {p["id"]: p for p in listing["projects"]}
        assert set(by_id) == {project_id(root), project_id(alpha)}
        assert by_id[project_id(alpha)]["session_count"] == 0
        assert by_id[project_id(alpha)]["live_sessions"] == 0
        assert by_id[project_id(root)]["name"] == "root"

        # Opening again is idempotent and reuses the manager.
        again = client.post("/api/projects/open", json={"path": str(alpha)}).json()
        assert again["id"] == info["id"]
        assert len(hub.managers) == 2

        assert client.post("/api/projects/open", json={"path": str(tmp_path / "gone")}).status_code == 400
        assert client.post("/api/projects/open", json={}).status_code == 400


def test_session_counts_reported_per_project(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    SessionStore(alpha, "aaaaaaaaaaaa").append_meta(title="one", model="test/model")
    SessionStore(alpha, "bbbbbbbbbbbb").append_meta(title="two", model="test/model")
    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        by_id = {p["id"]: p for p in client.get("/api/projects").json()["projects"]}
        assert by_id[project_id(alpha)]["session_count"] == 2
        assert by_id[project_id(root)]["session_count"] == 0


def test_unknown_project_id_is_404(tmp_path):
    hub, client = make_app(tmp_path, FakeProvider([]))
    with client:
        for path in ("bootstrap", "sessions", "models", "plugins"):
            assert client.get(f"/api/projects/deadbeef/{path}").status_code == 404
        assert client.post("/api/projects/deadbeef/conversations", json={}).status_code == 404


# ---- project-scoped conversations ----


def test_project_scoped_conversation_and_ws_turn(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    provider = FakeProvider([
        [TextDelta("Hello "), TextDelta("alpha"), Usage(input_tokens=3, output_tokens=2),
         TurnDone("stop")],
    ])
    hub, client = make_app(tmp_path, provider, default_dir=root)
    with client:
        pid = client.post("/api/projects/open", json={"path": str(alpha)}).json()["id"]

        bs = client.get(f"/api/projects/{pid}/bootstrap").json()
        assert bs["id"] == pid
        assert bs["cwd"] == str(alpha.resolve())
        assert bs["project"] == "alpha"

        conv_id = client.post(f"/api/projects/{pid}/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/projects/{pid}/conversation/{conv_id}") as ws:
            assert ws.receive_json()["type"] == "state"
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "hi"}))
            assert recv_until(ws, "user_message")["text"] == "hi"
            assert recv_until(ws, "assistant_message")["text"] == "Hello alpha"

            # Live means somebody is watching, so it is only true while the
            # socket above is still attached.
            live = client.get(f"/api/projects/{pid}/sessions").json()
            assert live[0]["live"] is True
            by_id = {p["id"]: p for p in client.get("/api/projects").json()["projects"]}
            assert by_id[pid]["live_sessions"] == 1

        # The transcript landed in the project, not in the launch directory.
        assert (alpha / ".quickcode" / "sessions" / f"{conv_id}.jsonl").exists()
        assert not (root / ".quickcode").exists()

        sessions = client.get(f"/api/projects/{pid}/sessions").json()
        assert [s["conv_id"] for s in sessions] == [conv_id]
        assert sessions[0]["live"] is False
        assert client.get("/api/sessions").json() == []

        models = client.get(f"/api/projects/{pid}/models").json()
        assert [m["id"] for m in models] == ["test/model"]
        assert {"read", "write", "bash"} <= {
            t["name"] for t in client.get(f"/api/projects/{pid}/plugins").json()["tools"]
        }


def test_ws_unknown_project_closes(tmp_path):
    hub, client = make_app(tmp_path, FakeProvider([]))
    with client, pytest.raises(WebSocketDisconnect):
        with ws_connect(client, "/ws/projects/deadbeef/conversation/abc") as ws:
            ws.receive_json()


# ---- session deletion ----


def test_delete_session(tmp_path):
    hub, client = make_app(tmp_path, FakeProvider([]))
    pid = hub.default_id
    with client:
        store = SessionStore(tmp_path, "deadbeef1234")
        store.append_meta(title="old", model="test/model")
        board_dir = tmp_path / ".quickcode" / "tasks" / "deadbeef1234"
        board_dir.mkdir(parents=True)
        (board_dir / "board.json").write_text("[]", encoding="utf-8")

        assert client.delete(f"/api/projects/{pid}/sessions/deadbeef1234").status_code == 204
        assert not store.path.exists()
        assert not board_dir.exists()

        # Gone, unknown, and traversal-shaped ids all 404.
        assert client.delete(f"/api/projects/{pid}/sessions/deadbeef1234").status_code == 404
        assert client.delete(f"/api/projects/{pid}/sessions/bad!id").status_code == 404
        assert client.delete("/api/projects/nosuch/sessions/deadbeef1234").status_code == 404


def test_delete_live_session_conflicts(tmp_path):
    """Live means watched. A window has to be open on it for 409 to be right."""
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    hub, client = make_app(tmp_path, provider)
    pid = hub.default_id
    with client:
        conv_id = client.post(f"/api/projects/{pid}/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/projects/{pid}/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            # A word in it, so there is a session on disk to argue about.
            ws.send_text(json.dumps({"type": "user_message", "text": "hi"}))
            recv_until(ws, "assistant_message")
            assert client.delete(f"/api/projects/{pid}/sessions/{conv_id}").status_code == 409
            assert (tmp_path / ".quickcode" / "sessions" / f"{conv_id}.jsonl").exists()

        # And once the window is gone it is nobody's session to protect.
        assert client.delete(f"/api/projects/{pid}/sessions/{conv_id}").status_code == 204


def test_opening_a_window_does_not_leave_a_session_behind(tmp_path):
    """Starting the app must not create anything.

    Opening a project opens a conversation, and that used to write its `meta`
    record immediately -- so seven launches left seven empty sessions in the
    list, and the "clean up N empty" button existed to sweep up after it.
    """
    hub, client = make_app(tmp_path, FakeProvider([]))
    pid = hub.default_id
    with client:
        conv_id = client.post(f"/api/projects/{pid}/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/projects/{pid}/conversation/{conv_id}") as ws:
            # Nothing to replay, and no system prompt either: a window that is
            # merely open has not sent anything, and the prompt is not final
            # until it does -- switching profile or composition re-renders it.
            replayed = _until_replay_done(ws)
            assert not any(e.get("ev", {}).get("type") == "system_prompt"
                           or e.get("type") == "system_prompt" for e in replayed)

        assert not (tmp_path / ".quickcode" / "sessions" / f"{conv_id}.jsonl").exists()
        assert client.get(f"/api/projects/{pid}/sessions").json() == []


def _until_replay_done(ws):
    """Every event a client receives up to and including replay_done."""
    seen = []
    while True:
        ev = ws.receive_json()
        seen.append(ev)
        if ev.get("type") == "replay_done":
            return seen


# ---- directory browsing ----


def test_dir_listing(tmp_path):
    root = tmp_path / "root"
    mkdirs(root / "visible", root / ".hidden", root / "repo" / ".git")
    (root / "file.txt").write_text("x", encoding="utf-8")
    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        data = client.get("/api/dir", params={"path": str(root)}).json()
        assert data["path"] == str(root.resolve())
        assert data["parent"] == str(tmp_path.resolve())
        assert [d["name"] for d in data["dirs"]] == ["repo", "visible"]
        assert [d["is_git"] for d in data["dirs"]] == [True, False]
        assert data["dirs"][0]["path"] == str((root / "repo").resolve())

        # A file is not a directory, and a missing path is not either.
        assert client.get("/api/dir", params={"path": str(root / "file.txt")}).status_code == 400
        assert client.get("/api/dir", params={"path": str(root / "nope")}).status_code == 400

        # No argument lands on the user's home directory.
        assert client.get("/api/dir").json()["path"] == str(Path.home().resolve())


def test_dir_listing_root_has_no_parent(tmp_path):
    anchor = Path(tmp_path.anchor)
    assert list_dirs(str(anchor))["parent"] is None


# ---- legacy aliases ----


def test_legacy_routes_address_the_default_project(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    mkdirs(root, alpha)
    hub, client = make_app(tmp_path, FakeProvider([]), default_dir=root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        assert client.get("/api/bootstrap").json()["cwd"] == str(root.resolve())
        assert client.get("/api/bootstrap").json() == {
            k: v for k, v in client.get(f"/api/projects/{hub.default_id}/bootstrap").json().items()
            if k != "id"
        }

        # A conversation opened through the legacy route belongs to the default
        # project and is reachable through both WS paths.
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        # Not a session yet: opening a window writes nothing. It becomes one
        # when somebody says something in it.
        assert client.get("/api/sessions").json() == []
        for path in (
            f"/ws/conversation/{conv_id}",
            f"/ws/projects/{hub.default_id}/conversation/{conv_id}",
        ):
            with ws_connect(client, path) as ws:
                assert ws.receive_json()["conv_id"] == conv_id


def test_create_app_accepts_a_bare_manager(tmp_path):
    """The single-project shape still boots and registers itself."""
    from quickcode.server.manager import ConversationManager
    from tests.test_server import make_env

    cfg = Config()
    cfg.last_model = "test/model"
    manager = ConversationManager(
        cwd=tmp_path, config=cfg, env=make_env(tmp_path), provider=FakeProvider([]),
    )
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    with TestClient(app, base_url="http://127.0.0.1:8642") as client:
        listing = client.get("/api/projects").json()
        assert [p["id"] for p in listing["projects"]] == [project_id(tmp_path)]
        pid = listing["projects"][0]["id"]
        assert client.get(f"/api/projects/{pid}/bootstrap").json()["cwd"] == str(tmp_path)
