"""Read-only git endpoints against a real throwaway repository."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import project_id


class FakeProvider:
    async def stream_chat(self, req):  # pragma: no cover - never driven here
        return
        yield

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def make_env(cwd: Path) -> Environment:
    return Environment(
        cwd=str(cwd), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=True, git_branch="",
    )


def make_manager(tmp_path: Path, provider) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    return ConversationManager(
        cwd=tmp_path, config=cfg, env=make_env(tmp_path), provider=provider,
    )


def make_client(manager) -> TestClient:
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    return TestClient(app, base_url="http://127.0.0.1:8642")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one committed-then-modified file and one untracked file."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-m", "init")
    (tmp_path / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "fresh.txt").write_text("brand new\n", encoding="utf-8")
    return tmp_path


def test_status_lists_changed_files(repo):
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get("/api/git/status").json()
    assert body["is_repo"] is True
    assert body["branch"] == "main"
    by_path = {f["path"]: f["status"] for f in body["files"]}
    assert by_path["tracked.txt"] == "M"
    assert by_path["fresh.txt"] == "??"


def test_status_staged_file_reports_added(repo):
    git(repo, "add", "fresh.txt")
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get("/api/git/status").json()
    by_path = {f["path"]: f["status"] for f in body["files"]}
    assert by_path["fresh.txt"] == "A"


def test_diff_of_tracked_change(repo):
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get("/api/git/diff", params={"path": "tracked.txt"}).json()
    assert body["path"] == "tracked.txt"
    assert body["truncated"] is False
    assert "+two" in body["diff"]


def test_diff_of_untracked_file_is_all_added(repo):
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get("/api/git/diff", params={"path": "fresh.txt"}).json()
    assert "+brand new" in body["diff"]


def test_diff_rejects_path_escape(repo):
    with make_client(make_manager(repo, FakeProvider())) as client:
        assert client.get("/api/git/diff", params={"path": "../outside.txt"}).status_code == 400
        assert client.get("/api/git/diff", params={"path": ""}).status_code == 400


def test_diff_of_unchanged_file_is_empty(repo):
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get("/api/git/diff", params={"path": "tracked.txt"}).json()
    assert body["diff"] == ""


def test_non_repo_reports_not_a_repo(tmp_path):
    with make_client(make_manager(tmp_path, FakeProvider())) as client:
        body = client.get("/api/git/status").json()
    assert body == {"is_repo": False, "branch": "", "files": []}


# ---- project-scoped aliases ----
# The UI addresses git by project id once a second project is open; the scoped
# routes must answer exactly what the unscoped ones answer for that project.


def test_project_scoped_status_matches_default(repo):
    pid = project_id(repo)
    with make_client(make_manager(repo, FakeProvider())) as client:
        scoped = client.get(f"/api/projects/{pid}/git/status")
        default = client.get("/api/git/status")
    assert scoped.status_code == 200
    assert scoped.json() == default.json()
    assert scoped.json()["branch"] == "main"


def test_project_scoped_diff_reads_that_project(repo):
    pid = project_id(repo)
    with make_client(make_manager(repo, FakeProvider())) as client:
        body = client.get(f"/api/projects/{pid}/git/diff", params={"path": "tracked.txt"}).json()
    assert body["path"] == "tracked.txt"
    assert "+two" in body["diff"]


def test_project_scoped_diff_rejects_path_escape(repo):
    pid = project_id(repo)
    with make_client(make_manager(repo, FakeProvider())) as client:
        res = client.get(f"/api/projects/{pid}/git/diff", params={"path": "../outside.txt"})
    assert res.status_code == 400


def test_project_scoped_git_404s_for_unknown_project(repo):
    with make_client(make_manager(repo, FakeProvider())) as client:
        assert client.get("/api/projects/deadbeef1234/git/status").status_code == 404
        assert client.get(
            "/api/projects/deadbeef1234/git/diff", params={"path": "tracked.txt"}
        ).status_code == 404


# ---- what a crafted path or a crafted repository must not reach ----


@pytest.fixture
def nested(repo: Path) -> Path:
    """A project that is a subdirectory of a larger repository, beside a
    changed file of that repository that lies outside the project."""
    (repo / "outside-secret.txt").write_text("keep out\n", encoding="utf-8")
    git(repo, "add", "outside-secret.txt")
    git(repo, "commit", "-m", "secret")
    (repo / "outside-secret.txt").write_text("keep out\nTOP SECRET CHANGE\n", encoding="utf-8")
    project = repo / "proj"
    project.mkdir()
    (project / "mine.txt").write_text("mine\n", encoding="utf-8")
    return project


@pytest.mark.parametrize("path", [
    ":(top)outside-secret.txt", ":/outside-secret.txt", ":(top,glob)**", ":/", ":(glob)../*",
])
def test_pathspec_magic_cannot_carry_a_diff_out_of_the_project(nested, path):
    """``_safe_rel`` proves a path is inside the project, but git reads a
    leading ``:`` as pathspec magic, and ``:(top)`` / ``:/`` mean the root of
    the *repository* -- which, for a project inside a larger repo, is above
    it."""
    with make_client(make_manager(nested, FakeProvider())) as client:
        res = client.get("/api/git/diff", params={"path": path})
    assert res.status_code in (200, 400)
    if res.status_code == 200:
        assert "TOP SECRET" not in res.json()["diff"]


@pytest.mark.parametrize("path", [
    "/etc/passwd", "//server/share/file", "\\\\server\\share\\file", "C:\\Windows\\win.ini",
    "C:/Windows/win.ini", "c:relative-to-drive.txt",
])
def test_an_absolute_drive_or_unc_path_is_refused_before_it_is_resolved(repo, path):
    """Resolving a UNC path is a network connection on Windows (and an NTLM
    handshake with whoever answers), so these are refused on their face."""
    with make_client(make_manager(repo, FakeProvider())) as client:
        assert client.get("/api/git/diff", params={"path": path}).status_code == 400


def _tripwire(repo: Path, name: str) -> tuple[Path, Path]:
    marker = repo.parent / f"{name}-ran"
    script = repo.parent / f"{name}.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    script.chmod(0o755)
    return script, marker


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the command")
def test_opening_the_git_panel_never_runs_a_command_the_repository_configured(repo):
    """A repository's own ``.git/config`` travels with a downloaded archive,
    and ``git status`` / ``git diff`` will run what it names: an fsmonitor
    hook, an external diff, a textconv driver. The panel is a viewer that
    runs as soon as a project opens, before anyone has trusted it."""
    fsmonitor, fs_ran = _tripwire(repo, "fsmonitor")
    external, ext_ran = _tripwire(repo, "external")
    textconv, tc_ran = _tripwire(repo, "textconv")
    git(repo, "config", "core.fsmonitor", str(fsmonitor))
    git(repo, "config", "diff.external", str(external))
    git(repo, "config", "diff.conv.textconv", str(textconv))
    (repo / ".gitattributes").write_text("*.txt diff=conv\n", encoding="utf-8")

    with make_client(make_manager(repo, FakeProvider())) as client:
        assert client.get("/api/git/status").json()["is_repo"] is True
        diff = client.get("/api/git/diff", params={"path": "tracked.txt"}).json()["diff"]
        client.get("/api/git/diff", params={"path": "fresh.txt"})

    assert "+two" in diff
    assert not fs_ran.exists(), "core.fsmonitor ran"
    assert not ext_ran.exists(), "diff.external ran"
    assert not tc_ran.exists(), "a textconv driver ran"
