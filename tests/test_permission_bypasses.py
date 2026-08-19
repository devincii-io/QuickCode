"""Four ways past the permission boundary, and the fixes that close them.

Each block below is a reproduction first and a regression test second: every
one of these failed before the corresponding fix and passes after it.

1. An environment-variable prefix (``PATH=. ls``) bought the silent read-only
   auto-allow, in every mode including ``plan``.
2. ``grep`` and ``glob`` omitted ``path_target``, so the protected-path check
   never ran for them and both read anywhere on the machine unprompted.
3. A cloned project's committed ``permissions.allow`` list took effect with no
   consent.
4. A cloned project's committed ``default_mode: "yolo"`` started the session in
   bypass mode.

3 and 4 are the same fix: project-scope configuration that can *widen* the
boundary goes through the trust gate the project's MCP servers and command
tools already go through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.core.permissions import (
    Decision,
    Mode,
    PermissionEngine,
    Rules,
)
from quickcode.kernel import preset as preset_module
from quickcode.kernel import state as state_store
from quickcode.kernel.resolve import default_mode
from quickcode.security import trust
from quickcode.tools import grep as grep_module
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.glob import GlobTool
from quickcode.tools.grep import GrepTool
from quickcode.tools.registry import default_registry


def engine(mode=Mode.ask, root=None, **kw):
    return PermissionEngine(mode=mode, rules=Rules(**kw), root=root or Path.cwd())


# --------------------------------------------------------------------------
# 1. env-var prefixes escape the command check
# --------------------------------------------------------------------------

def test_an_env_var_prefix_does_not_buy_the_read_only_auto_allow():
    # `ls` is a read-only builtin; `PATH=. ls` is a rewritten environment with
    # a read-only builtin standing in front of it.
    assert engine().evaluate("bash", "PATH=. ls") == Decision.ask
    assert engine(mode=Mode.plan).evaluate("bash", "PATH=. ls") == Decision.deny
    assert engine(mode=Mode.dontask).evaluate("bash", "PATH=. ls") == Decision.deny


def test_an_env_var_prefix_is_not_stripped_before_matching_an_allow_rule():
    # Approving `git status` is not approving `LD_PRELOAD=./evil.so git status`.
    e = engine(allow=["bash(git status)"])
    assert e.evaluate("bash", "git status") == Decision.allow
    assert e.evaluate("bash", "LD_PRELOAD=./evil.so git status") == Decision.ask


def test_an_env_var_prefix_still_hits_a_deny_rule():
    # The stripping that survives is the one on the deny side: a rule against
    # `rm` must not be dodged by putting an assignment in front of it.
    e = engine(deny=["bash(rm *)"])
    assert e.evaluate("bash", "FOO=1 rm -r build") == Decision.deny


def test_a_harmless_wrapper_still_auto_allows():
    # Only assignments lose the auto-allow. `timeout`/`nice` and friends do not
    # change the environment the command runs under.
    assert engine().evaluate("bash", "timeout ls") == Decision.allow
    assert engine().evaluate("bash", "nice cat file.txt") == Decision.allow


# --------------------------------------------------------------------------
# 2. grep and glob bypassed the protected-path check
# --------------------------------------------------------------------------

PATH_TARGET_FIELDS = {"path", "file_path", "dir", "directory", "file"}


def _decide(tool, args, mode=Mode.ask, root=None):
    return engine(mode=mode, root=root).evaluate_tool(tool, args)[0]


def test_grep_asks_before_reading_a_protected_path(tmp_path):
    # grep(output_mode="content") returns file *contents*; `read` on the same
    # path asks, so grep must ask too.
    secret = str(Path.home() / ".ssh")
    assert _decide(GrepTool(), {"pattern": "PRIVATE KEY", "path": secret},
                   root=tmp_path) == Decision.ask
    assert _decide(GrepTool(), {"pattern": "key", "path": str(tmp_path / ".env")},
                   root=tmp_path) == Decision.ask


def test_glob_asks_before_listing_outside_the_project(tmp_path):
    outside = str(tmp_path.parent / "elsewhere")
    assert _decide(GlobTool(), {"pattern": "**/*", "path": outside},
                   root=tmp_path) == Decision.ask


def test_grep_and_glob_inside_the_project_stay_unprompted(tmp_path):
    (tmp_path / "src").mkdir()
    assert _decide(GrepTool(), {"pattern": "x", "path": "src"},
                   root=tmp_path) == Decision.allow
    # No path at all means "the project", which is the common case.
    assert _decide(GrepTool(), {"pattern": "x"}, root=tmp_path) == Decision.allow
    assert _decide(GlobTool(), {"pattern": "**/*.py"}, root=tmp_path) == Decision.allow


async def test_a_project_wide_grep_does_not_return_the_contents_of_a_dotenv(tmp_path):
    """The gate prompts for a search that *names* a secret; a search that names
    the project names none of them, and must not sweep them up either."""
    (tmp_path / ".env").write_text("API_KEY=hunter2\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("API_KEY = os.environ['API_KEY']\n", encoding="utf-8")
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())

    swept = await GrepTool().run(
        GrepTool.Input(pattern="API_KEY", output_mode="content"), ctx)
    assert "hunter2" not in swept.content
    assert "app.py" in swept.content

    # Named explicitly it is searched, because by then the gate has asked.
    named = await GrepTool().run(
        GrepTool.Input(pattern="API_KEY", path=".env", output_mode="content"), ctx)
    assert "hunter2" in named.content


def test_the_ripgrep_path_excludes_the_same_secrets_the_walk_does(tmp_path, monkeypatch):
    """The two search backends must not disagree about what they will read."""
    seen: dict[str, list[str]] = {}

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b""

    def fake_run(args, **_kw):
        seen["args"] = args
        return _Proc()

    monkeypatch.setattr(grep_module.subproc, "run", fake_run)
    grep_module._run_ripgrep(
        "rg", GrepTool.Input(pattern="x", output_mode="content"), tmp_path)
    assert all(f"!{name}" in seen["args"] for name in (".env", ".env.*"))
    assert "!.ssh/**" in seen["args"]


def test_no_builtin_tool_targets_a_path_without_declaring_path_target():
    """The audit finding 2 came from, kept as a test so it cannot come back."""
    offenders = [
        name for name, tool in default_registry().tools.items()
        if tool.permission.target_field in PATH_TARGET_FIELDS
        and not tool.permission.path_target
    ]
    assert offenders == []


# --------------------------------------------------------------------------
# 3 + 4. project-scope config goes through the trust gate
# --------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory whose trust store and user config are both in tmp."""
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    return root


def _settings(root: Path, data: dict, *, local: bool = False) -> None:
    name = "settings.local.json" if local else "settings.json"
    (root / ".quickcode" / name).write_text(json.dumps(data), encoding="utf-8")


def test_an_untrusted_projects_committed_allow_rules_do_not_load(project):
    _settings(project, {"permissions": {"allow": ["bash(curl *)", "write"]}})
    assert Rules.load(project).allow == []


def test_an_untrusted_projects_local_allow_rules_do_not_load_either(project):
    # A repository can commit any filename it likes; gitignore is the
    # repository's own file, so "local" is a convention, not a provenance.
    _settings(project, {"permissions": {"allow": ["bash(curl *)"]}}, local=True)
    assert Rules.load(project).allow == []


def test_a_trusted_projects_allow_rules_load(project):
    _settings(project, {"permissions": {"allow": ["bash(uv run pytest*)"]}})
    trust.default_store().grant(project)
    assert Rules.load(project).allow == ["bash(uv run pytest*)"]


def test_an_untrusted_projects_deny_and_ask_rules_still_load(project):
    # Deny and ask only ever narrow, so honouring them from an untrusted
    # project costs nothing and refusing them would be a way to widen.
    _settings(project, {"permissions": {
        "allow": ["bash(curl *)"], "ask": ["bash(git push*)"], "deny": ["read(.env)"],
    }})
    rules = Rules.load(project)
    assert rules.allow == []
    assert rules.ask == ["bash(git push*)"]
    assert rules.deny == ["read(.env)"]


def test_an_untrusted_project_cannot_start_the_session_in_yolo(project):
    _settings(project, {"plugins": {
        "runtime.permissions": {"settings": {"default_mode": "yolo"}}
    }})
    assert default_mode(project) == "ask"


def test_a_trusted_project_may_set_the_default_mode(project):
    _settings(project, {"plugins": {
        "runtime.permissions": {"settings": {"default_mode": "yolo"}}
    }})
    trust.default_store().grant(project)
    assert default_mode(project) == "yolo"


def test_an_untrusted_project_may_still_ask_for_a_narrower_mode(project):
    # The gate is against a project widening the boundary. A repository that
    # ships "open me in plan mode" is being careful, and is honoured untrusted.
    _settings(project, {"plugins": {
        "runtime.permissions": {"settings": {"default_mode": "plan"}}
    }})
    assert default_mode(project) == "plan"
    assert state_store.untrusted_project_problems(project) == []


def test_an_untrusted_project_preset_cannot_set_the_default_mode(project):
    _settings(project, {
        "active_preset": "shipped",
        "presets": {"shipped": {"title": "Shipped", "default_mode": "yolo"}},
    })
    assert preset_module.resolve(project).default_mode == ""
    trust.default_store().grant(project)
    assert preset_module.resolve(project).default_mode == "yolo"


def test_the_trust_grant_is_bound_to_the_permission_settings_too(project):
    # Trusting a project for its MCP servers must not silently bless a
    # `default_mode: yolo` added to the same file afterwards.
    _settings(project, {"mcpServers": {"docs": {"command": "npx", "args": ["x"]}}})
    trust.default_store().grant(project)
    assert trust.is_trusted(project) is True

    _settings(project, {
        "mcpServers": {"docs": {"command": "npx", "args": ["x"]}},
        "plugins": {"runtime.permissions": {"settings": {"default_mode": "yolo"}}},
    })
    assert trust.is_trusted(project) is False
    assert default_mode(project) == "ask"


def test_a_refused_project_setting_is_reported_rather_than_silently_dropped(project):
    _settings(project, {
        "permissions": {"allow": ["bash(curl *)"]},
        "plugins": {"runtime.permissions": {"settings": {"default_mode": "yolo"}}},
    })
    problems = state_store.untrusted_project_problems(project)
    assert [p.code for p in problems] == ["project_settings_ignored"]
    message = problems[0].message
    assert "permissions.allow" in message
    assert "default_mode" in message
    assert problems[0].fix

    # And the trust report names them, so the banner has something to offer.
    status = trust.default_store().status(project)
    assert status.inert is True
    assert status.policy_keys


def test_a_project_with_no_security_settings_reports_nothing(project):
    _settings(project, {"plugins": {"runtime.compaction": {"settings": {"keep_turns": 4}}}})
    assert state_store.untrusted_project_problems(project) == []
    assert trust.default_store().status(project).inert is False
