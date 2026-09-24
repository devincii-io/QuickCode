"""A settings file saved by a Windows editor reads the same through every reader.

Notepad, PowerShell's ``Set-Content`` and Visual Studio save UTF-8 with a
byte-order mark, or UTF-16. ``json.loads`` refuses the mark, so such a file
read as ``{}`` -- its deny rules, MCP servers, hooks, presets and profiles all
silently gone -- and a UTF-16 one raised out of some readers while others
skipped it. The trust hash, the permission rules, the hooks and the MCP loader
each had a reader of their own, so they could disagree about one file: the
hash covering a file a loader then read differently is the security half of
the defect. Every reader is exercised here against the same bytes.
"""

from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from quickcode.config import Config
from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.hooks.config import load_hooks
from quickcode.kernel import state as state_store
from quickcode.kernel.settings_file import (
    SettingsUnreadable,
    read_settings,
    write_project_settings,
)
from quickcode.plugins import mcp
from quickcode.security import trust

ENCODINGS = {
    "utf-8-bom": lambda text: text.encode("utf-8-sig"),
    "utf-16-le": lambda text: codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
    "utf-16-be": lambda text: codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
    "utf-32": lambda text: codecs.BOM_UTF32_LE + text.encode("utf-32-le"),
}

SERVERS = {"docs": {"command": "npx", "args": ["-y", "docs-café"]}}
HOOKS = {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}
SETTINGS = {
    "mcpServers": SERVERS,
    "permissions": {"allow": ["bash(npm test)"], "deny": ["bash(rm café*)"]},
    "hooks": HOOKS,
    "plugins": {"tool.bash": {"settings": {"timeout_s": 42}}},
}
# Latin-1 "é" with no mark: not UTF-8, and nothing says what else it is.
NOT_UTF8 = '{"permissions": {"deny": ["bash(rm café*)"]}}'.encode("latin-1")


@pytest.fixture(params=sorted(ENCODINGS))
def encode(request):
    return ENCODINGS[request.param]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    return root


def _save(path: Path, data: dict, encode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(json.dumps(data, indent=2, ensure_ascii=False)))


def _project_file(root: Path, name: str = "settings.json") -> Path:
    return root / ".quickcode" / name


# ---------------------------------------------------------------- the readers


def test_the_settings_reader_reads_a_windows_saved_file(tmp_path, encode):
    path = tmp_path / "settings.json"
    _save(path, SETTINGS, encode)
    assert read_settings(path) == SETTINGS


def test_plugin_state_in_a_windows_saved_file_applies(project, encode):
    _save(_project_file(project), SETTINGS, encode)
    assert state_store.plugin_setting(project, "tool.bash", "timeout_s", trusted=True) == 42


def test_deny_rules_in_a_windows_saved_file_are_enforced(project, encode):
    _save(_project_file(project), SETTINGS, encode)

    rules = Rules.load(project, trusted=False)
    engine = PermissionEngine(Mode.yolo, rules, project)

    assert rules.deny == ["bash(rm café*)"]
    assert engine.evaluate("bash", "rm café.txt") == Decision.deny


def test_allow_rules_in_a_windows_saved_local_file_load_when_trusted(project, encode):
    _save(_project_file(project, "settings.local.json"), SETTINGS, encode)
    assert Rules.load(project, trusted=True).allow == ["bash(npm test)"]


def test_user_hooks_and_servers_in_a_windows_saved_file_load(encode):
    _save(state_store.user_settings_path(), SETTINGS, encode)

    assert mcp.user_server_configs() == SERVERS
    assert [h.command for h in load_hooks(None).hooks] == ["notify.sh"]


def test_the_user_config_saved_by_a_windows_editor_loads(tmp_path, encode):
    path = tmp_path / "config.json"
    _save(path, {"default_mode": "plan", "last_model": "vendor/modèle"}, encode)

    config = Config.load(path)

    assert (config.default_mode, config.last_model) == ("plan", "vendor/modèle")


# ------------------------------------------------- the gate and the runtime


def test_the_trust_hash_covers_what_the_runtime_then_loads(project, encode):
    """What the reviewer is shown and the grant binds is what then runs."""
    empty = trust.config_hash(project)
    _save(_project_file(project), SETTINGS, encode)

    status = trust.default_store().status(project)
    assert status.server_names == ["docs"]
    assert "permissions.allow" in status.policy_keys
    assert [row["command"] for row in status.hooks] == ["notify.sh"]
    assert status.config_hash != empty and not status.trusted

    trust.default_store().grant(project, expected=status.config_hash)

    assert mcp.project_server_configs(project) == SERVERS
    assert Rules.load(project).allow == ["bash(npm test)"]
    assert [h.command for h in load_hooks(project).hooks] == ["notify.sh"]

    # Still saved by the same editor: an edit moves the hash, as it would in UTF-8.
    _save(_project_file(project), {**SETTINGS, "mcpServers": {
        "docs": {"command": "evil", "args": []}}}, encode)
    assert not trust.is_trusted(project)


def test_every_reader_skips_a_file_with_no_mark_that_is_not_utf8(project, tmp_path):
    """Strict without a mark: no reader guesses a code page, none raises, and
    they all agree the file says nothing."""
    _project_file(project).write_bytes(NOT_UTF8)
    state_store.user_settings_path().write_bytes(NOT_UTF8)

    assert read_settings(_project_file(project)) == {}
    assert Rules.load(project, trusted=True).deny == []
    assert trust.default_store().status(project).policy_keys == []
    assert mcp.load_server_configs(project) == {}
    assert load_hooks(project, trusted=True).hooks == ()
    assert state_store.load_state(project, trusted=True) == {}


def test_utf16_without_a_mark_is_not_guessed_at(project):
    _project_file(project).write_bytes(json.dumps(SETTINGS).encode("utf-16-le"))
    assert read_settings(_project_file(project)) == {}
    assert trust.project_mcp_servers(project) == {}


# ---------------------------------------------------------------- the writer


def test_a_save_merges_into_a_windows_saved_file(project, encode):
    _save(_project_file(project), SETTINGS, encode)

    write_project_settings(project, lambda raw: raw.update(active_preset="minimal"))

    raw = _project_file(project).read_bytes()
    assert json.loads(raw.decode("utf-8")) == {**SETTINGS, "active_preset": "minimal"}


def test_a_save_still_refuses_a_file_that_is_not_utf8(project):
    _project_file(project).write_bytes(NOT_UTF8)
    with pytest.raises(SettingsUnreadable, match="UTF-8"):
        write_project_settings(project, lambda raw: raw.update(active_preset="x"))
    assert _project_file(project).read_bytes() == NOT_UTF8
