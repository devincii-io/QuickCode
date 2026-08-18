"""Session transcripts must not be one ``git add -A`` away from a commit."""

from __future__ import annotations

import subprocess

from quickcode.core.tasks import TaskBoard
from quickcode.providers.base import ChatMessage
from quickcode.session.store import SessionStore
from quickcode.subagents.artifacts import write_artifact
from quickcode.workspace import ensure_project_dir


def _gitignore(root):
    return root / ".quickcode" / ".gitignore"


def test_writing_a_session_creates_a_gitignore_beside_it(tmp_path):
    SessionStore(tmp_path, conv_id="conv-1").append_message(
        ChatMessage(role="user", content="hello")
    )
    assert _gitignore(tmp_path).is_file()


def test_the_gitignore_covers_everything_that_holds_conversation_content(tmp_path):
    ensure_project_dir(tmp_path)
    patterns = [
        line.strip()
        for line in _gitignore(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "sessions/" in patterns
    assert "tasks/" in patterns
    assert "artifacts/" in patterns
    assert "settings.local.json" in patterns


def test_the_gitignore_leaves_project_shared_config_committable(tmp_path):
    ensure_project_dir(tmp_path)
    patterns = [
        line.strip()
        for line in _gitignore(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # settings.json declares permissions and MCP servers for the whole team,
    # and authored agents/plugins are shared by design.
    assert "settings.json" not in patterns
    assert "agents/" not in patterns
    assert "plugins/" not in patterns


def test_the_gitignore_says_what_it_is_protecting(tmp_path):
    ensure_project_dir(tmp_path)
    header = _gitignore(tmp_path).read_text(encoding="utf-8").split("\n\n")[0]
    assert header.startswith("#")
    assert "QuickCode" in header
    assert "sessions/" in header


def test_an_existing_gitignore_is_left_exactly_as_the_user_wrote_it(tmp_path):
    (tmp_path / ".quickcode").mkdir()
    _gitignore(tmp_path).write_text("# mine\n!sessions/shared.jsonl\n", encoding="utf-8")

    SessionStore(tmp_path, conv_id="conv-2").append_message(
        ChatMessage(role="user", content="hello")
    )
    ensure_project_dir(tmp_path)

    assert _gitignore(tmp_path).read_text(encoding="utf-8") == "# mine\n!sessions/shared.jsonl\n"


def test_a_project_that_is_not_a_git_repository_still_works(tmp_path):
    assert not (tmp_path / ".git").exists()
    store = SessionStore(tmp_path, conv_id="conv-3")
    store.append_message(ChatMessage(role="user", content="hello"))
    assert store.load_messages()[0].content == "hello"
    assert _gitignore(tmp_path).is_file()


def test_a_task_board_write_also_guards_the_directory(tmp_path):
    board = TaskBoard.load(tmp_path / ".quickcode" / "tasks" / "conv-4" / "board.json")
    board.create("do the thing")
    assert _gitignore(tmp_path).is_file()


def test_an_offloaded_subagent_report_also_guards_the_directory(tmp_path):
    assert write_artifact(tmp_path, "explore-1", "a long report") is not None
    assert _gitignore(tmp_path).is_file()


def test_ensure_project_dir_for_ignores_a_path_outside_any_project_dir(tmp_path):
    from quickcode.workspace import ensure_project_dir_for

    assert ensure_project_dir_for(tmp_path / "notes" / "board.json") is None
    assert not (tmp_path / ".quickcode").exists()


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def test_git_actually_ignores_the_transcripts(tmp_path):
    """The end the whole thing exists for: `git add -A` must not stage a log."""
    if _git(tmp_path, "init", "-q").returncode != 0:
        import pytest

        pytest.skip("git not available")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")

    SessionStore(tmp_path, conv_id="conv-5").append_message(
        ChatMessage(role="user", content="a secret from the transcript")
    )
    (tmp_path / ".quickcode" / "settings.json").write_text("{}", encoding="utf-8")
    _git(tmp_path, "add", "-A")

    staged = _git(tmp_path, "diff", "--cached", "--name-only").stdout.splitlines()
    assert ".quickcode/sessions/conv-5.jsonl" not in staged
    assert ".quickcode/settings.json" in staged
