"""Path completion for the composer's @ token, against a real throwaway tree.

The route's whole job is to answer inside one project root and nowhere else, so
most of what is asserted here is a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.paths import MAX_RESULTS
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
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )


def make_manager(cwd: Path) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    return ConversationManager(
        cwd=cwd, config=cfg, env=make_env(cwd), provider=FakeProvider(),
    )


def make_client(manager) -> TestClient:
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    # TestClient sends Host: testserver by default; use an allowed host.
    return TestClient(app, base_url="http://127.0.0.1:8642")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A project with the shapes that matter: nesting, secrets, junk, dotfiles."""
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("SECRET=2\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

    src = tmp_path / "src"
    src.mkdir()
    (src / "composer.js").write_text("//\n", encoding="utf-8")
    (src / "compat.js").write_text("//\n", encoding="utf-8")
    (src / "deep").mkdir()
    (src / "deep" / "buried.txt").write_text("x\n", encoding="utf-8")

    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text("[core]\n", encoding="utf-8")
    qc = tmp_path / ".quickcode"
    qc.mkdir()
    (qc / "settings.json").write_text("{}\n", encoding="utf-8")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("key\n", encoding="utf-8")

    nm = tmp_path / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("//\n", encoding="utf-8")

    (tmp_path.parent / "outside.txt").write_text("not yours\n", encoding="utf-8")
    return tmp_path


def get(client, **params) -> dict:
    res = client.get("/api/paths", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def paths_of(body: dict) -> list[str]:
    return [row["path"] for row in body["paths"]]


def test_an_empty_query_lists_the_project_root_and_not_the_whole_tree(tree):
    with make_client(make_manager(tree)) as client:
        got = paths_of(get(client, q=""))
    assert "README.md" in got
    assert "src" in got
    assert "src/composer.js" not in got  # a listing, not a recursive search


def test_a_prefix_query_returns_only_the_entries_that_start_with_it(tree):
    with make_client(make_manager(tree)) as client:
        got = paths_of(get(client, q="src/compo"))
    assert got == ["src/composer.js"]


def test_a_bare_name_finds_a_file_nested_deeper_in_the_tree(tree):
    with make_client(make_manager(tree)) as client:
        got = paths_of(get(client, q="buried"))
    assert "src/deep/buried.txt" in got


def test_a_directory_is_marked_as_one_so_the_menu_can_keep_descending(tree):
    with make_client(make_manager(tree)) as client:
        by_path = {r["path"]: r["is_dir"] for r in get(client, q="src")["paths"]}
    assert by_path["src"] is True
    assert by_path["src/composer.js"] is False


def test_a_query_that_escapes_the_project_root_is_refused(tree):
    with make_client(make_manager(tree)) as client:
        res = client.get("/api/paths", params={"q": "../outside.txt"})
    assert res.status_code == 400
    assert "escapes" in res.json()["detail"]


def test_a_query_that_escapes_by_a_long_way_round_is_refused_too(tree):
    with make_client(make_manager(tree)) as client:
        res = client.get("/api/paths", params={"q": "src/../../outside"})
    assert res.status_code == 400


def test_an_absolute_path_cannot_be_used_to_read_another_directory(tree):
    with make_client(make_manager(tree)) as client:
        res = client.get("/api/paths", params={"q": str(tree.parent) + "/"})
    # Either refused outright or resolved back inside the root — never a
    # listing of the parent directory.
    if res.status_code == 200:
        assert "outside.txt" not in paths_of(res.json())
    else:
        assert res.status_code == 400


def test_the_git_and_quickcode_and_ssh_directories_are_never_offered(tree):
    with make_client(make_manager(tree)) as client:
        listing = paths_of(get(client, q=""))
        dotted = paths_of(get(client, q="."))
    for name in (".git", ".quickcode", ".ssh"):
        assert name not in listing
        assert name not in dotted


def test_the_contents_of_a_blocked_directory_are_not_reachable_by_asking(tree):
    with make_client(make_manager(tree)) as client:
        assert paths_of(get(client, q=".git/")) == []
        assert paths_of(get(client, q=".ssh/")) == []
        assert paths_of(get(client, q=".quickcode/")) == []


def test_an_env_file_is_never_offered_even_when_it_is_typed_out(tree):
    with make_client(make_manager(tree)) as client:
        assert paths_of(get(client, q=".env")) == []
        assert paths_of(get(client, q=".env.local")) == []


def test_a_dotfile_stays_hidden_until_the_query_reaches_for_it(tree):
    with make_client(make_manager(tree)) as client:
        assert ".gitignore" not in paths_of(get(client, q=""))
        assert ".gitignore" in paths_of(get(client, q=".gitig"))


def test_the_ignored_directories_the_glob_tool_skips_are_skipped_here_too(tree):
    with make_client(make_manager(tree)) as client:
        got = paths_of(get(client, q="index"))
    assert got == []


def test_the_result_count_is_capped(tmp_path: Path):
    bulk = tmp_path / "many"
    bulk.mkdir()
    for i in range(MAX_RESULTS + 40):
        (bulk / f"file{i:04d}.txt").write_text("x\n", encoding="utf-8")
    with make_client(make_manager(tmp_path)) as client:
        body = get(client, q="many/file")
    assert len(body["paths"]) == MAX_RESULTS
    assert body["truncated"] is True


def test_a_smaller_limit_is_honoured_and_says_the_answer_was_cut(tmp_path: Path):
    for i in range(10):
        (tmp_path / f"note{i}.md").write_text("x\n", encoding="utf-8")
    with make_client(make_manager(tmp_path)) as client:
        body = get(client, q="note", limit=3)
    assert len(body["paths"]) == 3
    assert body["truncated"] is True


def test_the_project_scoped_twin_answers_the_same_paths_as_the_unscoped_route(tree):
    pid = project_id(tree)
    with make_client(make_manager(tree)) as client:
        unscoped = get(client, q="src/compo")
        scoped = client.get(f"/api/projects/{pid}/paths", params={"q": "src/compo"})
    assert scoped.status_code == 200
    assert paths_of(scoped.json()) == paths_of(unscoped)


def test_an_unknown_project_id_is_a_404_rather_than_a_listing(tree):
    with make_client(make_manager(tree)) as client:
        res = client.get("/api/projects/deadbeef1234/paths", params={"q": ""})
    assert res.status_code == 404
