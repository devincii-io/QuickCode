"""Read-only git endpoints against a real throwaway repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager


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
