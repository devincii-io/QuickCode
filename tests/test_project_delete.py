"""Removing a project from the list, and deleting QuickCode's data for it.

These two acts are deliberately different, and most of what is asserted here is
about the second one *not* happening: the project directory survives, a crafted
path cannot reach outside ``<project>/.quickcode``, a symlink is refused rather
than followed, and a bulk delete says which rows it could not do.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config
from quickcode.security.trust import TrustStore
from quickcode.server.app import create_app
from quickcode.server.projects import ProjectHub, ProjectRegistry, project_id
from quickcode.session.store import (
    SessionStore,
    project_data_dir,
    project_data_summary,
    purge_project_data,
)
from tests.test_server import FakeProvider, recv_until, ws_connect


async def _no_mcp(cwd):
    return [], []


def make_app(tmp_path: Path, default_dir: Path):
    """A hub whose registry *and* trust store live in the temp directory.

    The trust store is injected because a purge revokes a grant, and a test that
    reached the real ``~/.quickcode/trust.json`` would be deleting the user's.
    """
    cfg = Config()
    cfg.last_model = "test/model"
    hub = ProjectHub(
        config=cfg,
        provider=FakeProvider([]),
        registry=ProjectRegistry(tmp_path / "projects.json"),
        mcp_connect=_no_mcp,
        trust_store=TrustStore(tmp_path / "trust.json"),
    )
    asyncio.run(hub.open(default_dir, make_default=True))
    app = create_app(hub, host="127.0.0.1", port=8642, token="")
    return hub, TestClient(app, base_url="http://127.0.0.1:8642")


def populate(root: Path) -> dict[str, Path]:
    """A project with source code beside a fully furnished .quickcode."""
    root.mkdir(parents=True, exist_ok=True)
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "main.py").write_text("print('mine')", encoding="utf-8")
    (root / "README.md").write_text("# mine", encoding="utf-8")
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    SessionStore(root, "aaaaaaaaaaaa").append_meta(title="one", model="test/model")
    SessionStore(root, "bbbbbbbbbbbb").append_meta(title="two", model="test/model")
    board = root / ".quickcode" / "tasks" / "aaaaaaaaaaaa"
    board.mkdir(parents=True, exist_ok=True)
    (board / "board.json").write_text("[]", encoding="utf-8")
    artifacts = root / ".quickcode" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "explore-1.md").write_text("notes", encoding="utf-8")
    (root / ".quickcode" / "settings.json").write_text("{}", encoding="utf-8")
    return {"src": src, "data": root / ".quickcode"}


def can_symlink(tmp_path: Path) -> bool:
    """Windows needs a privilege for this; skip rather than pretend to test it."""
    probe, target = tmp_path / "_probe-link", tmp_path / "_probe-target"
    target.mkdir(exist_ok=True)
    try:
        probe.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


# ---- the containment proof: what must never happen ----


def test_purging_project_data_leaves_the_project_directory_and_its_source_files_alone(tmp_path):
    root = tmp_path / "proj"
    paths = populate(root)

    result = purge_project_data(root)

    assert result.removed is True
    assert not paths["data"].exists()
    # The whole point: everything that is the user's is still there.
    assert root.is_dir()
    assert (root / "README.md").read_text(encoding="utf-8") == "# mine"
    assert (paths["src"] / "main.py").read_text(encoding="utf-8") == "print('mine')"
    assert (root / ".git" / "HEAD").exists()
    assert sorted(p.name for p in root.iterdir()) == [".git", "README.md", "src"]


def test_the_only_deletable_directory_is_the_quickcode_child_of_the_resolved_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = project_data_dir(root)
    assert target == Path(os.path.realpath(root)) / ".quickcode"
    assert target.parent == Path(os.path.realpath(root))
    # Never the root, never a sibling, never an ancestor.
    assert target != Path(os.path.realpath(root))
    assert str(target).startswith(str(Path(os.path.realpath(root))) + os.sep)


@pytest.mark.parametrize("crafted", ["..", "../..", "src/../..", ".", ".quickcode/.."])
def test_a_crafted_project_path_can_never_resolve_outside_its_own_quickcode(tmp_path, crafted):
    """A traversal-shaped root still yields that root's own .quickcode.

    The dangerous reading would be "join and hope"; ``project_data_dir``
    resolves first, so whatever directory the crafted string lands on, the answer
    is always exactly that directory's own ``.quickcode`` — never its parent,
    and never the directory itself.
    """
    (tmp_path / "proj" / ".quickcode").mkdir(parents=True)
    (tmp_path / "proj" / "src").mkdir()
    raw = tmp_path / "proj" / crafted
    if not raw.exists():
        pytest.skip("crafted path does not name a directory here")
    target = project_data_dir(raw)
    base = Path(os.path.realpath(raw))
    assert target == base / ".quickcode"
    assert target != base
    assert target.parent == base


def test_purging_refuses_a_symlinked_quickcode_directory_and_deletes_what_it_points_at(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("no symlink privilege on this machine")
    root = tmp_path / "proj"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep me", encoding="utf-8")
    (root / ".quickcode").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ValueError, match="link"):
        project_data_dir(root)
    with pytest.raises(ValueError, match="link"):
        purge_project_data(root)

    assert (elsewhere / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert (root / ".quickcode").is_symlink()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows thing")
def test_purging_refuses_a_quickcode_directory_junction_and_deletes_what_it_points_at(tmp_path):
    """The Windows shape of the same attack, and the one that actually runs here.

    A junction needs no privilege to create, and ``Path.is_symlink()`` answers
    *False* for one — so this is the case that proves the refusal is carried by
    the realpath comparison rather than by the symlink check.
    """
    root = tmp_path / "proj"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep me", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / ".quickcode"), str(elsewhere)],
        capture_output=True,
    )
    if made.returncode != 0:  # pragma: no cover - environment without mklink
        pytest.skip("could not create a directory junction here")
    assert (root / ".quickcode").is_symlink() is False  # the trap this closes

    with pytest.raises(ValueError):
        project_data_dir(root)
    with pytest.raises(ValueError):
        purge_project_data(root)

    assert (elsewhere / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_purging_refuses_when_quickcode_is_a_file_rather_than_a_directory(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".quickcode").write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError):
        purge_project_data(root)
    assert (root / ".quickcode").read_text(encoding="utf-8") == "not a directory"


def test_purging_refuses_a_root_that_is_not_a_directory_at_all(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(ValueError):
        purge_project_data(missing)
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        purge_project_data(a_file)


def test_purging_a_project_that_has_no_quickcode_directory_is_not_an_error(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    result = purge_project_data(root)
    assert result.existed is False
    assert result.removed is False
    assert result.path.endswith(".quickcode")
    assert root.is_dir()


def test_the_data_summary_counts_what_a_purge_would_remove_and_names_the_path(tmp_path):
    root = tmp_path / "proj"
    populate(root)
    SessionStore(root, "cccccccccccc").append_meta(title="three", model="test/model")
    SessionStore(root, "cccccccccccc").archive()

    summary = project_data_summary(root)
    assert summary["exists"] is True
    assert summary["path"] == str(Path(os.path.realpath(root)) / ".quickcode")
    assert summary["sessions"] == 2
    assert summary["archived"] == 1
    assert summary["boards"] == 1
    assert summary["artifacts"] == 1
    assert summary["bytes"] > 0
    assert "settings.json" in summary["entries"]


# ---- removing from the list: the safe one ----


def test_removing_a_project_from_the_list_leaves_every_file_on_disk_alone(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    root.mkdir()
    paths = populate(alpha)
    hub, client = make_app(tmp_path, root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        pid = project_id(alpha)

        result = client.delete(f"/api/projects/{pid}")
        assert result.status_code == 200
        body = result.json()
        assert body["removed_from_list"] is True
        assert body["data_deleted"] is False
        assert body["data_dir"] is None

        assert pid not in {p["id"] for p in client.get("/api/projects").json()["projects"]}
        # Not one byte moved.
        assert paths["data"].is_dir()
        assert (paths["data"] / "settings.json").exists()
        assert (alpha / "README.md").exists()
        assert SessionStore(alpha, "aaaaaaaaaaaa").path.exists()

        # And opening it again brings it back exactly as it was.
        client.post("/api/projects/open", json={"path": str(alpha)})
        listing = client.get(f"/api/projects/{pid}/sessions").json()
        assert {s["conv_id"] for s in listing} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_removing_a_project_that_is_not_on_the_list_is_a_404(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hub, client = make_app(tmp_path, root)
    with client:
        assert client.delete("/api/projects/deadbeefcafe").status_code == 404
        assert client.delete("/api/projects/deadbeefcafe/data").status_code == 404
        assert client.get("/api/projects/deadbeefcafe/data").status_code == 404


def test_an_idle_open_project_is_closed_when_it_is_removed_but_the_default_is_kept(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    root.mkdir()
    alpha.mkdir()
    hub, client = make_app(tmp_path, root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        assert client.delete(f"/api/projects/{project_id(alpha)}").json()["closed"] is True
        assert project_id(alpha) not in hub.managers

        # The default project is what this process is serving: its entry goes,
        # its manager stays, and the unscoped routes keep answering.
        assert client.delete(f"/api/projects/{hub.default_id}").json()["closed"] is False
        assert client.get("/api/bootstrap").status_code == 200


# ---- deleting QuickCode's data: the "completely" one ----


def test_deleting_quickcode_data_removes_that_directory_the_trust_grant_and_the_list_entry(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    root.mkdir()
    paths = populate(alpha)
    hub, client = make_app(tmp_path, root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        pid = project_id(alpha)
        hub._trust_store.grant(alpha)
        assert hub._trust_store.recorded_hash(alpha) is not None

        body = client.delete(f"/api/projects/{pid}/data").json()
        assert body["data_deleted"] is True
        assert body["data_existed"] is True
        assert body["trust_revoked"] is True
        assert body["removed_from_list"] is True
        assert body["data_dir"] == str(Path(os.path.realpath(alpha)) / ".quickcode")

        assert not paths["data"].exists()
        assert hub._trust_store.recorded_hash(alpha) is None
        # Everything the user owns is untouched.
        assert alpha.is_dir()
        assert (alpha / "README.md").exists()
        assert (paths["src"] / "main.py").exists()
        assert (alpha / ".git").is_dir()


def test_the_scoped_and_unscoped_data_routes_answer_for_the_same_directory(tmp_path):
    root = tmp_path / "root"
    populate(root)
    hub, client = make_app(tmp_path, root)
    with client:
        unscoped = client.get("/api/data").json()
        scoped = client.get(f"/api/projects/{hub.default_id}/data").json()
        assert unscoped == scoped
        assert unscoped["path"] == str(Path(os.path.realpath(root)) / ".quickcode")
        assert unscoped["sessions"] == 2


# ---- live conversations ----


def test_a_project_with_a_live_conversation_refuses_both_removal_and_data_deletion(tmp_path):
    root, alpha = tmp_path / "root", tmp_path / "alpha"
    root.mkdir()
    paths = populate(alpha)
    hub, client = make_app(tmp_path, root)
    with client:
        client.post("/api/projects/open", json={"path": str(alpha)})
        pid = project_id(alpha)
        conv_id = client.post(
            f"/api/projects/{pid}/conversations", json={}
        ).json()["conv_id"]

        # Attached: a window is watching this conversation, so both refuse.
        with ws_connect(client, f"/ws/projects/{pid}/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/data"):
                res = client.delete(path)
                assert res.status_code == 409, path
                assert "live" in res.json()["detail"]

            # Refused means nothing happened, to the list or to the disk.
            assert pid in {p["id"] for p in client.get("/api/projects").json()["projects"]}
            assert paths["data"].is_dir()

        # And once nobody is watching, the same call goes through: a project the
        # user merely visited used to be undeletable for the life of the process.
        assert client.delete(f"/api/projects/{pid}").status_code == 200


# ---- bulk: honest per-item reporting ----


def test_removing_several_projects_reports_each_one_that_could_not_be_removed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alpha, beta, busy = tmp_path / "alpha", tmp_path / "beta", tmp_path / "busy"
    for d in (alpha, beta, busy):
        d.mkdir()
    hub, client = make_app(tmp_path, root)
    with client:
        for d in (alpha, beta, busy):
            client.post("/api/projects/open", json={"path": str(d)})
        bpid = project_id(busy)
        conv = client.post(f"/api/projects/{bpid}/conversations", json={}).json()["conv_id"]

        # Busy means *attached*. Merely having opened a conversation once used
        # to count, for the life of the process, so a project the user had
        # visited could never be removed again.
        with ws_connect(client, f"/ws/projects/{bpid}/conversation/{conv}") as ws:
            recv_until(ws, "replay_done")
            body = client.post("/api/projects/remove", json={
                "ids": [project_id(alpha), bpid, "deadbeefcafe", project_id(beta)],
            }).json()

            assert [r["id"] for r in body["removed"]] == [project_id(alpha), project_id(beta)]
            assert [(s["id"], s["reason"]) for s in body["skipped"]] == [
                (bpid, "live"),
                ("deadbeefcafe", "unknown"),
            ]
            # The two that worked are gone; the busy one is still listed.
            listed = {p["id"] for p in client.get("/api/projects").json()["projects"]}
            assert project_id(alpha) not in listed
            assert bpid in listed


def test_a_bulk_purge_deletes_only_the_data_of_the_projects_it_reports(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    alpha, busy = tmp_path / "alpha", tmp_path / "busy"
    populate(alpha)
    populate(busy)
    hub, client = make_app(tmp_path, root)
    with client:
        for d in (alpha, busy):
            client.post("/api/projects/open", json={"path": str(d)})
        bpid = project_id(busy)
        conv = client.post(f"/api/projects/{bpid}/conversations", json={}).json()["conv_id"]

        with ws_connect(client, f"/ws/projects/{bpid}/conversation/{conv}") as ws:
            recv_until(ws, "replay_done")
            body = client.post("/api/projects/purge", json={
                "ids": [project_id(alpha), bpid],
            }).json()

            assert [r["id"] for r in body["removed"]] == [project_id(alpha)]
            assert body["skipped"][0]["reason"] == "live"
            assert not (alpha / ".quickcode").exists()
            assert (busy / ".quickcode").is_dir()
            # Neither project directory itself was touched.
            assert (alpha / "README.md").exists()
            assert (busy / "README.md").exists()


def test_a_bulk_session_delete_reports_the_live_ones_as_skipped_rather_than_failing_whole(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hub, client = make_app(tmp_path, root)
    pid = hub.default_id
    with client:
        for conv_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
            SessionStore(root, conv_id).append_meta(title=conv_id, model="test/model")
        live = client.post(f"/api/projects/{pid}/conversations", json={}).json()["conv_id"]

        # Attached, so it is genuinely in use for the length of this block.
        with ws_connect(client, f"/ws/projects/{pid}/conversation/{live}") as ws:
            recv_until(ws, "replay_done")
            body = client.post(f"/api/projects/{pid}/sessions/delete", json={
                "conv_ids": ["aaaaaaaaaaaa", live, "bbbbbbbbbbbb", "dddddddddddd"],
            }).json()

            assert sorted(body["deleted"]) == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
            reasons = {s["conv_id"]: s["reason"] for s in body["skipped"]}
            assert reasons == {live: "live", "dddddddddddd": "missing"}
            assert SessionStore(root, "cccccccccccc").path.exists()
            assert SessionStore(root, live).path.exists()


def test_a_bulk_request_without_a_selection_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hub, client = make_app(tmp_path, root)
    with client:
        for path in ("/api/projects/remove", "/api/projects/purge"):
            assert client.post(path, json={}).status_code == 400
            assert client.post(path, json={"ids": []}).status_code == 400
            assert client.post(path, json={"ids": [1, 2]}).status_code == 400


def test_a_junction_inside_the_data_directory_stops_the_purge(tmp_path: Path) -> None:
    """The containment check proves `.quickcode` is inside the project. It says
    nothing about what is inside `.quickcode` — and `shutil.rmtree` recurses
    into a Windows directory junction, which reports as an ordinary directory.
    A link in there would carry the delete out of the project entirely, so it
    refuses rather than guessing.
    """
    project = tmp_path / "proj"
    (project / ".quickcode" / "artifacts").mkdir(parents=True)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete me", encoding="utf-8")

    link = project / ".quickcode" / "artifacts" / "linked"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True, text=True,
    )
    if made.returncode != 0 or not link.exists():
        pytest.skip(f"could not create a junction here: {made.stderr.strip()}")

    with pytest.raises(ValueError, match="points outside"):
        purge_project_data(project)
    assert (outside / "keep.txt").exists(), "the junction's target was deleted"
    assert (project / ".quickcode").is_dir(), "the data directory went anyway"
