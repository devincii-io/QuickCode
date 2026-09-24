"""``POST /api/permissions/explain``: the gate's answer, from the gate.

Every case asks the endpoint and then asks the engine of a session that was
really opened in the same project -- ``manager.open()``, the same rules, the
same profile -- and requires the two to agree. The endpoint is only useful if
it cannot say something the gate would not do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.core.permissions import Mode, PermissionEngine
from quickcode.kernel import state as state_store
from quickcode.security import trust
from quickcode.server.projects import project_id
from tests.test_server import FakeProvider, make_client, make_manager

PROJECT = {
    "permissions": {
        "deny": ["bash(git push**)", "read(**.pem)", "read(**id_rsa)", "bash(shred **)"],
        "ask": ["bash(npm publish**)"],
    },
}
LOCAL = {
    "permissions": {
        "allow": ["bash(git **)", "bash(npm *)", "edit(src/**)", "read(**.pem)"],
    },
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def _write(root: Path, name: str, data: dict) -> None:
    (root / ".quickcode" / name).write_text(json.dumps(data), encoding="utf-8")


def _setup(root: Path, *, trusted: bool = True, extra: dict | None = None) -> None:
    _write(root, "settings.json", {**PROJECT, **(extra or {})})
    _write(root, "settings.local.json", LOCAL)
    if trusted:
        trust.default_store().grant(root)


def _real_decision(manager, conv_id: str, tool: str, args: dict, mode: str | None) -> str:
    """What the gate of a really opened session says about this call."""
    live = manager.get(conv_id).agent.permissions
    engine = PermissionEngine(
        mode=Mode(mode) if mode else live.mode, rules=live.rules, root=live.root,
        yolo_accepted=live.yolo_accepted, specs=live.specs,
    )
    return engine.evaluate_tool(manager.registry_factory().get(tool), args)[0].value


# (tool, arguments, mode, expected decision, step that decided)
MATRIX = [
    # deny beats allow across scopes: the deny is in settings.json, the allow
    # in settings.local.json -- and for .pem, the very same pattern is in both.
    ("bash", {"command": "git push origin main"}, "ask", "deny", "deny_rule"),
    ("bash", {"command": "git push origin main"}, "yolo", "deny", "deny_rule"),
    ("read", {"file_path": "keys/server.pem"}, "ask", "deny", "deny_rule"),
    ("bash", {"command": "git status"}, "ask", "allow", "allow_rule"),
    ("edit", {"file_path": "src/app.py"}, "ask", "allow", "allow_rule"),
    # protected paths: ahead of every allow rule, a refusal in dontask, waived in yolo
    ("read", {"file_path": ".env"}, "ask", "ask", "protected_path"),
    ("read", {"file_path": ".env"}, "dontask", "deny", "protected_path"),
    ("read", {"file_path": ".env"}, "yolo", "allow", "read_only"),
    ("edit", {"file_path": ".git/config"}, "auto-edit", "ask", "protected_path"),
    ("bash", {"command": "cat ~/.ssh/id_rsa"}, "ask", "ask", "protected_path"),
    ("bash", {"command": "cat $HOME/.aws/credentials"}, "dontask", "deny", "protected_path"),
    # ...but a deny rule and plan mode both answer before the protected prompt
    ("read", {"file_path": ".ssh/id_rsa"}, "ask", "deny", "deny_rule"),
    ("edit", {"file_path": ".git/config"}, "plan", "deny", "plan_mode"),
    # a command another command runs is judged as if typed
    ("bash", {"command": "find . -name '*.tmp' -exec shred {} +"}, "yolo", "deny", "deny_rule"),
    ("bash", {"command": "$TOOL build"}, "ask", "ask", "unresolvable_command"),
    # circuit breakers prompt in yolo -- and a deny rule still beats them
    ("bash", {"command": "rm -rf /"}, "yolo", "ask", "circuit_breaker"),
    ("bash", {"command": ":(){ :|:& };:"}, "yolo", "ask", "circuit_breaker"),
    ("bash", {"command": "git push --force origin main"}, "yolo", "deny", "deny_rule"),
    # compound commands: judged per subcommand, most restrictive wins
    ("bash", {"command": "npm test && rm -rf build"}, "auto-edit", "ask", "mode_default"),
    ("bash", {"command": "npm test && npm publish"}, "ask", "ask", "ask_rule"),
    ("bash", {"command": "git status && ls -la"}, "ask", "allow", "most_restrictive"),
    ("bash", {"command": "ls && git push origin main"}, "ask", "deny", "deny_rule"),
    # read-only auto-allow, and what plan mode leaves of the shell
    ("read", {"file_path": "src/app.py"}, "ask", "allow", "read_only"),
    ("read", {"file_path": "src/app.py"}, "plan", "allow", "read_only"),
    ("bash", {"command": "ls -la src"}, "plan", "allow", "readonly_builtin"),
    ("bash", {"command": "npm install"}, "plan", "deny", "plan_mode"),
    ("edit", {"file_path": "src/app.py"}, "plan", "deny", "plan_mode"),
    ("bash", {"command": "echo hi > out.txt"}, "ask", "ask", "mode_default"),
    ("bash", {"command": "make"}, "dontask", "deny", "mode_default"),
]


def test_every_case_agrees_with_the_engine_of_a_really_opened_session(project):
    _setup(project)
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        for tool, args, mode, expected, step in MATRIX:
            answer = client.post("/api/permissions/explain",
                                 json={"tool": tool, "input": args, "mode": mode})
            assert answer.status_code == 200, (tool, args, answer.text)
            payload = answer.json()
            real = _real_decision(manager, conv_id, tool, args, mode)
            case = (tool, args, mode)
            assert payload["decision"] == real, case
            assert payload["decision"] == expected, case
            assert payload["decided_by"]["step"] == step, (case, payload["decided_by"])
            assert payload["posture"]["mode"] == mode
            assert payload["summary"], case


def test_a_matched_rule_names_the_file_it_came_from(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        denied = client.post("/api/permissions/explain", json={
            "tool": "read", "input": {"file_path": "k.pem"}, "mode": "ask",
        }).json()
        allowed = client.post("/api/permissions/explain", json={
            "command": "git status", "mode": "ask",
        }).json()

    assert denied["decided_by"]["rule"] == "read(**.pem)"
    assert denied["decided_by"]["sources"] == [
        {"scope": "project", "source": ".quickcode/settings.json"},
    ]
    rule = allowed["steps"][1]["steps"][-1]
    assert rule["rule"] == "bash(git **)"
    assert rule["sources"] == [{"scope": "project", "source": ".quickcode/settings.local.json"}]


def test_a_compound_command_is_broken_down_per_subcommand(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "command": "npm test && rm -rf build", "mode": "auto-edit",
        }).json()

    subs = [s for s in payload["steps"] if s["step"] == "subcommand"]
    assert [(s["command"], s["decision"]) for s in subs] == [
        ("npm test", "allow"), ("rm -rf build", "ask"),
    ]
    assert subs[0]["steps"][-1]["rule"] == "bash(npm *)"
    assert payload["decided_by"]["command"] == "rm -rf build"
    assert payload["summary"].startswith("'rm -rf build'")
    # "Always allow" writes an exact rule for the half that asked, and none
    # for the half a rule already allows -- and says the call would then run.
    assert payload["suggestion"]["rules"] == ["bash(rm -rf build)"]
    assert payload["suggestion"]["rule"] == "bash(rm -rf build)"
    assert payload["suggestion"]["next_time"] == "allow"
    assert "bash(rm -rf build)" in payload["suggestion"]["text"]


def test_a_command_run_by_another_is_named_as_what_decided(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "command": "find . -name '*.tmp' -exec shred {} +", "mode": "yolo",
        }).json()

    inner = [s for s in payload["steps"] if s["step"] == "inner_command"]
    assert inner and inner[0]["decision"] == "deny"
    assert payload["decided_by"]["rule"] == "bash(shred **)"
    assert payload["decided_by"]["command"].startswith("shred")
    assert "shred" in payload["summary"]


def test_always_allow_on_a_protected_path_is_said_not_to_help(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "tool": "read", "target": ".env", "mode": "ask",
        }).json()

    assert payload["decision"] == "ask"
    # No rule is offered for a path that asks whatever is saved.
    assert payload["suggestion"]["rules"] == []
    assert payload["suggestion"]["kept"] == [{"part": ".env", "reason": "protected_path"}]
    assert payload["suggestion"]["file"] == ".quickcode/settings.local.json"
    assert payload["suggestion"]["persists"] is True
    assert payload["suggestion"]["next_time"] == "ask"
    assert payload["suggestion"]["text"].startswith("Always allow would save nothing")


def test_only_a_prompt_offers_an_always_allow(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        for body in ({"command": "git status"}, {"command": "git push"}):
            payload = client.post("/api/permissions/explain", json={**body, "mode": "ask"}).json()
            assert payload["decision"] != "ask"
            assert payload["suggestion"] is None


def test_without_a_mode_it_answers_for_the_mode_a_new_session_starts_in(project):
    """The posture is a new session's, profile included -- checked against one."""
    _setup(project, extra={"active_profile": "readonly"})
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        payload = client.post("/api/permissions/explain", json={
            "tool": "edit", "input": {"file_path": "src/app.py"},
        }).json()
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        live = manager.get(conv_id).agent.permissions

    assert payload["posture"]["mode"] == live.mode.value == "plan"
    assert payload["posture"]["mode_source"] == "new session"
    assert payload["posture"]["profile"] == "readonly"
    assert payload["decision"] == "deny"
    assert any("withheld" in n for n in payload["notes"])


def test_a_profile_deny_beats_a_project_allow_and_says_so(project):
    _setup(project, extra={"active_profile": "readonly"})
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        payload = client.post("/api/permissions/explain", json={
            "tool": "edit", "input": {"file_path": "src/app.py"}, "mode": "ask",
        }).json()
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        real = _real_decision(manager, conv_id, "edit", {"file_path": "src/app.py"}, "ask")

    assert payload["decision"] == real == "deny"
    assert payload["decided_by"]["rule"] == "edit"
    assert payload["decided_by"]["sources"] == [
        {"scope": "profile", "source": "readonly (built-in)"},
    ]


def test_an_untrusted_projects_allow_rules_are_ignored_and_the_hint_says_so(project):
    _setup(project, trusted=False)
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        payload = client.post("/api/permissions/explain", json={
            "command": "git status", "mode": "ask",
        }).json()
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        real = _real_decision(manager, conv_id, "bash", {"command": "git status"}, "ask")

    assert payload["decision"] == real == "ask"
    assert payload["posture"]["trusted"] is False
    [hint] = payload["hints"]
    assert hint["kind"] == "untrusted_allow"
    assert hint["decision"] == "allow"
    assert hint["rules"] == [{"rule": "bash(git **)", "source": ".quickcode/settings.local.json"}]
    assert payload["suggestion"]["persists"] is False


def test_a_live_conversation_is_asked_with_what_it_approved_during_the_session(project):
    _setup(project)
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        before = client.post("/api/permissions/explain", json={
            "command": "make test", "conv": conv_id,
        }).json()
        # What answering "Always allow" leaves behind in an untrusted-or-not
        # session's live engine, without the settings file round trip.
        manager.get(conv_id).agent.permissions.rules.allow.append("bash(make *)")
        after = client.post("/api/permissions/explain", json={
            "command": "make test", "conv": conv_id,
        }).json()

    assert before["decision"] == "ask"
    assert before["posture"]["mode_source"] == "session"
    assert before["posture"]["conv"] == conv_id
    assert after["decision"] == "allow"
    assert after["decided_by"]["sources"] == [
        {"scope": "session", "source": "approved during this session"},
    ]


def test_a_live_conversation_is_asked_from_where_its_shell_stands(project):
    """The loop hands the gate the shell's cwd after a `cd`; so must a dry run,
    or `ls` reads as harmless in a session whose shell has left the project."""
    _setup(project)
    manager = make_manager(project, FakeProvider([]))
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        fresh = client.post("/api/permissions/explain", json={
            "command": "ls", "conv": conv_id, "mode": "ask",
        }).json()
        agent = manager.get(conv_id).agent
        agent.ctx.extra["bash_cwd"] = project.parent
        moved = client.post("/api/permissions/explain", json={
            "command": "ls", "conv": conv_id, "mode": "ask",
        }).json()
        live = agent.permissions
        bash = agent.registry.get("bash")
        real = live.evaluate_tool(bash, {"command": "ls"}, cwd=project.parent)[0].value

    assert fresh["decision"] == "allow"
    assert moved["decision"] == real == "ask"
    assert moved["decided_by"]["reason"] == "cwd"
    assert moved["posture"]["shell_cwd"] == str(project.parent)


def test_a_dry_run_changes_nothing(project):
    _setup(project)
    before = sorted(p.relative_to(project) for p in project.rglob("*"))
    local = (project / ".quickcode" / "settings.local.json").read_text("utf-8")
    with make_client(make_manager(project, FakeProvider([]))) as client:
        for tool, args, mode, _, _ in MATRIX:
            client.post("/api/permissions/explain",
                        json={"tool": tool, "input": args, "mode": mode})

    assert sorted(p.relative_to(project) for p in project.rglob("*")) == before
    assert (project / ".quickcode" / "settings.local.json").read_text("utf-8") == local
    assert trust.is_trusted(project)


def test_the_project_scoped_route_answers_for_that_project(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        pid = project_id(project)
        payload = client.post(f"/api/projects/{pid}/permissions/explain", json={
            "command": "git push", "mode": "ask",
        }).json()
        missing = client.post("/api/projects/000000000000/permissions/explain",
                              json={"command": "ls"})

    assert payload["decision"] == "deny"
    assert missing.status_code == 404


@pytest.mark.parametrize(("body", "status"), [
    ({"tool": "nope", "target": "x"}, 404),
    ({"command": "ls", "mode": "sideways"}, 400),
    ({"tool": "read", "command": "cat x"}, 400),
    ({"tool": "read", "input": ["x"]}, 400),
    ({"tool": "read", "input": {}, "target": "x"}, 400),
    ({"command": "ls", "conv": "not-open"}, 404),
    ({"target": "x"}, 400),
    ({"command": 3}, 400),
])
def test_malformed_requests_are_refused_with_a_reason(project, body, status):
    with make_client(make_manager(project, FakeProvider([]))) as client:
        answer = client.post("/api/permissions/explain", json=body)
    assert answer.status_code == status
    assert answer.json()["detail"]


def test_a_body_that_is_not_an_object_is_refused(project):
    with make_client(make_manager(project, FakeProvider([]))) as client:
        assert client.post("/api/permissions/explain", json=["ls"]).status_code == 400
        assert client.post("/api/permissions/explain", content=b"{").status_code == 400


# ---------------------------------------------------------------------------
# what-ifs: rules written nowhere yet, and layers left out
# ---------------------------------------------------------------------------

def test_what_if_rules_join_the_real_ones_and_deny_still_wins(project):
    from quickcode.core.profiles import PermissionProfile

    _setup(project)
    manager = make_manager(project, FakeProvider([]))
    extra = {"deny": ["bash(git status**)"], "allow": ["bash(make**)"]}
    with make_client(manager) as client:
        denied = client.post("/api/permissions/explain", json={
            "command": "git status", "mode": "ask", "rules": extra,
        }).json()
        allowed = client.post("/api/permissions/explain", json={
            "command": "make test", "mode": "ask", "rules": extra,
        }).json()
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        live = manager.get(conv_id).agent.permissions

    # The same merge a profile gets, asked of the same engine.
    merged = PermissionProfile.from_dict("x", extra).merged(live.rules)
    engine = PermissionEngine(mode=Mode.ask, rules=merged, root=live.root, specs=live.specs)
    bash = manager.registry_factory().get("bash")
    assert denied["decision"] == engine.evaluate_tool(bash, {"command": "git status"})[0].value
    assert allowed["decision"] == engine.evaluate_tool(bash, {"command": "make test"})[0].value

    assert denied["decision"] == "deny"
    assert denied["decided_by"]["sources"] == [
        {"scope": "what-if", "source": "added for this question"},
    ]
    assert allowed["decision"] == "allow"


def test_leaving_the_project_rules_out_answers_on_the_rest(project):
    _setup(project)
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "command": "git push origin main", "mode": "ask", "project_rules": False,
        }).json()

    assert payload["decision"] == "ask"
    assert payload["decided_by"]["step"] == "mode_default"
    assert payload["posture"]["project_rules"] is False


def test_leaving_the_profile_out_answers_on_the_project_alone(project):
    _setup(project, extra={"active_profile": "readonly"})
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "tool": "edit", "target": "src/app.py", "mode": "ask", "profile": False,
        }).json()

    assert payload["posture"]["profile"] == ""
    assert payload["decision"] == "allow"
    assert payload["decided_by"]["rule"] == "edit(src/**)"


def test_a_what_if_rule_the_engine_could_never_match_is_reported(project):
    with make_client(make_manager(project, FakeProvider([]))) as client:
        payload = client.post("/api/permissions/explain", json={
            "command": "ls", "rules": {"deny": ["bash(ls **", "bash(rm **)"]},
        }).json()

    assert payload["invalid_rules"] == ["deny: bash(ls **"]


@pytest.mark.parametrize("body", [
    {"command": "ls", "rules": {"allow": "bash(ls)"}},
    {"command": "ls", "rules": {"sometimes": []}},
    {"command": "ls", "rules": ["bash(ls)"]},
    {"command": "ls", "project_rules": "no"},
])
def test_malformed_what_ifs_are_refused(project, body):
    with make_client(make_manager(project, FakeProvider([]))) as client:
        answer = client.post("/api/permissions/explain", json=body)
    assert answer.status_code == 400
    assert answer.json()["detail"]


def test_a_live_conversation_cannot_leave_its_own_rules_out(project):
    with make_client(make_manager(project, FakeProvider([]))) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        answer = client.post("/api/permissions/explain", json={
            "command": "ls", "conv": conv_id, "project_rules": False,
        })
    assert answer.status_code == 400
