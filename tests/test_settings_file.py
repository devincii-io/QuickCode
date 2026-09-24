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
