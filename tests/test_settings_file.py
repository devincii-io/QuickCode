"""Every write the app makes to a project's settings file keeps the project's trust.

The trust hash covers presets, profiles, plugin state and permission rules, all
of which live in ``.quickcode/settings.json``. A save made from the app's own
UI is not a reason to stop trusting the project; before one writer owned that
file, some routes re-bound the grant after writing and some did not, so saving
a composition or deleting a profile silently switched off the project's allow
rules and MCP servers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.kernel import state as state_store
from quickcode.security import trust
from tests.test_server import FakeProvider, make_client, make_manager

MCP = {"docs": {"command": "npx", "args": ["-y", "docs"]}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    return root


def _settings(root: Path, data: dict) -> None:
    (root / ".quickcode" / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def _read(root: Path) -> dict:
    return json.loads((root / ".quickcode" / "settings.json").read_text("utf-8"))


def _client(root: Path):
    return make_client(make_manager(root, FakeProvider([])))


def test_deriving_a_composition_keeps_a_trusted_project_trusted(project):
    _settings(project, {"mcpServers": MCP, "presets": {
        "wide": {"title": "Wide", "default_mode": "auto-edit"},
    }})
    trust.default_store().grant(project)

    with _client(project) as client:
        answer = client.post("/api/kernel/compositions/wide/derive",
                             json={"name": "Wider"})
        assert answer.status_code == 200, answer.text

    assert "wider" in _read(project)["presets"]
    assert trust.is_trusted(project)


def test_deleting_a_widening_profile_keeps_a_trusted_project_trusted(project):
    _settings(project, {"mcpServers": MCP, "profiles": {
        "generous": {"title": "Generous", "allow": ["bash(**)"]},
    }})
    trust.default_store().grant(project)

    with _client(project) as client:
        answer = client.delete("/api/profiles/generous?scope=project")
        assert answer.status_code == 200, answer.text

    assert "generous" not in _read(project).get("profiles", {})
    assert trust.is_trusted(project)


def test_saving_a_profile_keeps_a_trusted_project_trusted(project):
    _settings(project, {"mcpServers": MCP})
    trust.default_store().grant(project)

    with _client(project) as client:
        answer = client.post("/api/profiles", json={
            "id": "wide", "title": "Wide", "allow": ["bash(**)"], "scope": "project",
        })
        assert answer.status_code == 200, answer.text

    assert trust.is_trusted(project)


def test_a_save_never_trusts_an_untrusted_project(project):
    _settings(project, {"mcpServers": MCP, "presets": {
        "wide": {"title": "Wide", "default_mode": "auto-edit"},
    }})

    with _client(project) as client:
        assert client.post("/api/kernel/compositions/wide/derive",
                           json={"name": "Wider"}).status_code == 200
        assert client.post("/api/profiles", json={
            "id": "wide", "title": "Wide", "allow": ["bash(**)"], "scope": "project",
        }).status_code == 200

    assert not trust.is_trusted(project)


def test_always_allow_and_a_plugin_knob_keep_a_trusted_project_trusted(project):
    from quickcode.core.permissions import Rules

    _settings(project, {"mcpServers": MCP})
    trust.default_store().grant(project)

    Rules().persist_allow(project, "bash(npm test)")
    state_store.save_entry(project, "runtime.permissions",
                           settings={"default_mode": "auto-edit"})

    local = json.loads((project / ".quickcode" / "settings.local.json").read_text("utf-8"))
    assert local["permissions"]["allow"] == ["bash(npm test)"]
    assert _read(project)["mcpServers"] == MCP
    assert trust.is_trusted(project)


def test_a_write_keeps_the_keys_it_does_not_own(project):
    from quickcode.kernel.settings_file import write_project_settings

    _settings(project, {"mcpServers": MCP, "hooks": {"x": 1}})
    write_project_settings(project, lambda raw: raw.update(presets={}))
    assert _read(project) == {"mcpServers": MCP, "hooks": {"x": 1}, "presets": {}}


BROKEN = '{"mcpServers": {"docs": {"command": "npx"}}, "permissions": {"allow": ["read"]},}'


def test_a_settings_file_that_does_not_parse_is_never_overwritten(project):
    """A stray comma in a hand-edited file used to read as ``{}``, and the next
    save wrote back only the key it owned -- every permission rule, MCP server
    and hook in the file gone, without a word."""
    from quickcode.kernel.settings_file import SettingsUnreadable, write_project_settings

    path = project / ".quickcode" / "settings.json"
    path.write_text(BROKEN, encoding="utf-8")

    with pytest.raises(SettingsUnreadable, match="settings.json"):
        write_project_settings(project, lambda raw: raw.update(active_preset="x"))
    with pytest.raises(SettingsUnreadable):
        state_store.save_entry(project, "tool.bash", enabled=False)

    assert path.read_text(encoding="utf-8") == BROKEN


def test_the_settings_page_says_why_it_will_not_save_over_a_broken_file(project):
    path = project / ".quickcode" / "settings.json"
    path.write_text(BROKEN, encoding="utf-8")

    with _client(project) as client:
        answer = client.put("/api/kernel/plugins/tool.bash", json={"enabled": False})

    assert answer.status_code == 400
    assert "not valid JSON" in answer.json()["detail"]
    assert path.read_text(encoding="utf-8") == BROKEN


def test_always_allow_over_a_broken_file_still_allows_for_this_session(project):
    from quickcode.core.permissions import Rules

    path = project / ".quickcode" / "settings.local.json"
    path.write_text(BROKEN, encoding="utf-8")
    rules = Rules()

    rules.persist_allow(project, "bash(npm test)")

    assert rules.allow == ["bash(npm test)"]
    assert path.read_text(encoding="utf-8") == BROKEN


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/api/presets/active", {"preset": "minimal"}),
    ("POST", "/api/kernel/compositions/standard/derive", {"name": "Mine"}),
    ("POST", "/api/profiles", {"id": "mine", "title": "Mine", "scope": "project"}),
    ("POST", "/api/profiles/active", {"id": "readonly"}),
])
def test_a_save_over_a_broken_file_is_refused_with_the_reason_not_a_500(
        project, method, path, body):
    """400 rather than 409: these routes use 409 for "confirm and resend", and
    resending cannot fix a file only the user can."""
    settings = project / ".quickcode" / "settings.json"
    settings.write_text(BROKEN, encoding="utf-8")

    with _client(project) as client:
        answer = client.request(method, path, json=body)

    assert answer.status_code == 400, answer.text
    assert "not valid JSON" in answer.json()["detail"]
    assert settings.read_text(encoding="utf-8") == BROKEN


def _unwritable(project: Path) -> None:
    """``.quickcode`` as a file: every write under it fails with an OSError, as
    in a read-only checkout, whoever runs the test."""
    (project / ".quickcode").rmdir()
    (project / ".quickcode").write_text("", encoding="utf-8")


def test_always_allow_in_a_project_that_cannot_be_written_allows_for_this_session(project):
    from quickcode.core.permissions import Rules

    _unwritable(project)
    rules = Rules()

    unsaved = rules.persist_allow(project, "bash(npm test)")

    assert rules.allow == ["bash(npm test)"]
    assert "bash(npm test)" in unsaved and "this session" in unsaved


def test_always_allow_that_saves_has_nothing_to_report(project):
    from quickcode.core.permissions import Rules

    assert Rules().persist_allow(project, "bash(npm test)") is None


async def test_a_rule_that_cannot_be_saved_does_not_fail_the_call_it_allowed(project):
    """The user said yes, so the call runs: failing to keep the rule for next
    time is no reason to refuse the call they just approved."""
    from quickcode.core.agent import PermissionOutcome
    from quickcode.core.events import TurnDone
    from quickcode.core.permissions import Mode
    from tests.test_loop import Scripted, _agent, _call, _tool_messages

    _unwritable(project)
    target = project / "notes.txt"
    provider = Scripted([[_call("w", "write", file_path=str(target), content="kept"),
                          TurnDone("tool_calls")]])
    agent = _agent(project, provider, mode=Mode.ask)

    async def always(_req):
        return PermissionOutcome(allow=True, persist=True)
    agent.permission_cb = always

    await agent.run_turn("write it")

    assert target.read_text(encoding="utf-8") == "kept"
    assert "failed" not in _tool_messages(agent)["w"]
    assert len(agent.permissions.rules.allow) == 1


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks need privileges this machine does not grant")


def test_the_users_own_settings_file_is_written_through_a_symlink(tmp_path):
    from quickcode.kernel.settings_file import write_settings

    dotfiles = tmp_path / "dotfiles" / "settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text("{}", encoding="utf-8")
    link = tmp_path / "settings.json"
    _symlink(link, dotfiles)

    write_settings(link, lambda raw: raw.update(active_preset="x"))

    assert link.is_symlink()
    assert json.loads(dotfiles.read_text("utf-8")) == {"active_preset": "x"}


def test_a_projects_settings_symlink_cannot_redirect_the_write(project, tmp_path):
    """The link is the repository's: following it would let a cloned project
    point the app's next save at any file the user can write."""
    from quickcode.kernel.settings_file import write_project_settings

    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    _symlink(project / ".quickcode" / "settings.json", elsewhere)

    write_project_settings(project, lambda raw: raw.update(active_preset="x"))

    assert elsewhere.read_text(encoding="utf-8") == "{}"
    assert not (project / ".quickcode" / "settings.json").is_symlink()
    assert _read(project) == {"active_preset": "x"}


# ---- a permissions block that is not the shape it should be ----


@pytest.mark.parametrize("block", [
    ["bash(*)"], "bash(*)", 3, {"allow": "bash(*)", "deny": "rm"}, {"deny": [3, None]},
])
def test_a_malformed_permissions_block_is_reported_rather_than_crashing_the_open(
        project, caplog, block):
    """``"permissions": [...]`` used to raise out of ``Rules.load`` -- the
    ``.get`` was outside the ``try`` -- and a string where a list belongs was
    read one character per rule."""
    from quickcode.core.permissions import Rules

    _settings(project, {"permissions": block})
    (project / ".quickcode" / "settings.local.json").write_text(
        json.dumps({"permissions": {"deny": ["write"]}}), encoding="utf-8")

    rules = Rules.load(project, trusted=True)

    assert rules.allow == [] and rules.ask == []
    assert rules.deny == ["write"]  # the file that is well formed still counts
    assert "settings.json" in caplog.text and "permissions" in caplog.text


@pytest.mark.parametrize("block", [["bash(*)"], {"allow": "bash(ls)"}])
def test_always_allow_over_a_malformed_permissions_block_says_why_it_was_not_saved(
        project, block):
    from quickcode.core.permissions import Rules

    local = project / ".quickcode" / "settings.local.json"
    local.write_text(json.dumps({"permissions": block}), encoding="utf-8")
    rules = Rules()

    unsaved = rules.persist_allow(project, "bash(npm test)")

    assert rules.allow == ["bash(npm test)"]
    assert unsaved and "this session" in unsaved and "permissions" in unsaved
    assert json.loads(local.read_text(encoding="utf-8")) == {"permissions": block}
