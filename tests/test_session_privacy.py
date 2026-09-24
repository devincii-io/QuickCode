"""Session transcripts must not be one ``git add -A`` away from a commit."""

from __future__ import annotations

import json
import subprocess

from quickcode import secrets
from quickcode.core.tasks import TaskBoard
from quickcode.providers.base import ChatMessage, ProviderError
from quickcode.session.store import SessionStore
from quickcode.subagents.artifacts import write_artifact
from quickcode.workspace import ensure_project_dir
from tests.test_server import make_client, make_manager, recv_until, ws_connect


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
    assert "checkpoints/" in patterns
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


# ---- credentials never reach the log ----
#
# The transcript is gitignored, but it is still a plain file in the project,
# attached to bug reports and read by anything that indexes the tree. The
# provider key travels in an Authorization header, and every error path that
# embeds exception text -- a turn failure, a failed compaction, a tool error --
# writes whatever the HTTP client or the server put in that text. A proxy that
# echoes the rejected header is enough to put the key on disk for good.

PROVIDER_KEY = "sk-or-v1-" + "0123456789abcdef" * 4


def _log_text(root) -> str:
    sessions = root / ".quickcode" / "sessions"
    return "".join(p.read_text(encoding="utf-8") for p in sessions.glob("*.jsonl"))


class _EchoingProxy:
    """A provider endpoint that answers 401 by echoing the header it refused."""

    def __init__(self, echoed: str):
        self.echoed = echoed

    async def stream_chat(self, req):
        raise ProviderError(
            "Error code: 401 - {'error': {'message': 'Authentication Error, invalid "
            f"proxy token. Received Authorization: {self.echoed}'}}}}"
        )
        yield  # pragma: no cover - makes this an async generator

    async def list_models(self):
        return []


def _run_one_failing_turn(tmp_path, provider) -> dict:
    manager = make_manager(tmp_path, provider)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        with ws_connect(client, f"/ws/conversation/{conv_id}") as ws:
            recv_until(ws, "replay_done")
            ws.send_text(json.dumps({"type": "user_message", "text": "hi"}))
            return recv_until(ws, "error")


def test_the_provider_key_echoed_in_an_error_never_reaches_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("QUICKCODE_OPENROUTER_API_KEY", PROVIDER_KEY)
    live = _run_one_failing_turn(tmp_path, _EchoingProxy(f"Bearer {PROVIDER_KEY}"))

    text = _log_text(tmp_path)
    assert "Authentication Error" in text
    assert PROVIDER_KEY not in text
    # What the window showed live is what a replay will show.
    assert PROVIDER_KEY not in live["message"]
    assert "[redacted]" in live["message"]


def test_a_saved_key_is_redacted_as_well_as_one_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("QUICKCODE_OPENROUTER_API_KEY", raising=False)
    secrets.save_api_key(PROVIDER_KEY)
    brave = "BSA" + "q" * 30
    secrets.save_secret("search-brave", brave)

    store = SessionStore(tmp_path, "conv")
    store.append_event({"type": "tool_result", "id": "c1", "name": "bash",
                        "content": f"OPENROUTER={PROVIDER_KEY}\nBRAVE={brave}",
                        "is_error": False, "ms": 1})
    store.append_message(ChatMessage(role="tool", content=f"key is {PROVIDER_KEY}",
                                     tool_call_id="c1", name="bash"))
    text = _log_text(tmp_path)
    assert PROVIDER_KEY not in text
    assert brave not in text
    assert text.count("[redacted]") == 3


def test_an_authorization_header_in_error_text_is_redacted_whoever_it_belongs_to(tmp_path):
    # Not a key QuickCode knows about -- a custom endpoint's own token, say --
    # but error text is where credentials leak, so their shapes are scrubbed.
    _run_one_failing_turn(tmp_path, _EchoingProxy("Bearer abcDEF0123456789token"))
    text = _log_text(tmp_path)
    assert "abcDEF0123456789token" not in text
    assert "Authorization: Bearer [redacted]'" in text


def test_credentials_in_a_failed_tool_result_url_are_redacted(tmp_path):
    store = SessionStore(tmp_path, "conv")
    url = "https://user:hunter2hunter2@api.example.com/v1?api_key=zzzzzzzzzzzz&q=1"
    store.append_event({"type": "tool_result", "id": "c1", "name": "web_fetch",
                        "content": f"request to {url} failed", "is_error": True, "ms": 1})
    store.append_message(ChatMessage(role="tool", content=f"[error] request to {url} failed",
                                     tool_call_id="c1", name="web_fetch"))
    text = _log_text(tmp_path)
    assert "hunter2hunter2" not in text
    assert "zzzzzzzzzzzz" not in text
    assert "q=1" in text  # only the credential goes, not the rest of the URL


def test_ordinary_transcript_text_is_left_alone(tmp_path, monkeypatch):
    # Pattern scrubbing is for error text. A file the model read that happens
    # to contain a bearer header in example code is the user's own content, and
    # replaying it altered would misreport what the model was shown.
    monkeypatch.setenv("QUICKCODE_OPENROUTER_API_KEY", PROVIDER_KEY)
    store = SessionStore(tmp_path, "conv")
    code = 'headers = {"Authorization": "Bearer example-token-123456"}'
    store.append_event({"type": "tool_result", "id": "c1", "name": "read",
                        "content": code, "is_error": False, "ms": 1})
    events = SessionStore(tmp_path, "conv").load_events()
    assert events[0]["content"] == code
