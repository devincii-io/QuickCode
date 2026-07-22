from pathlib import Path

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules, next_mode


def engine(mode=Mode.ask, root=None, **kw):
    return PermissionEngine(mode=mode, rules=Rules(**kw), root=root or Path.cwd())


def test_readonly_builtin_auto_allows():
    e = engine()
    assert e.evaluate("bash", "ls -la") == Decision.allow
    assert e.evaluate("bash", "cat file.txt") == Decision.allow


def test_mutating_bash_prompts_in_ask():
    e = engine()
    assert e.evaluate("bash", "npm install") == Decision.ask


def test_deny_beats_allow():
    e = engine(allow=["bash(rm *)"], deny=["bash(rm *)"])
    assert e.evaluate("bash", "rm foo") == Decision.deny


def test_compound_command_not_prefix_matched():
    # allow rule for `git status` must NOT green-light `git status && rm -rf x`
    e = engine(allow=["bash(git status)"])
    assert e.evaluate("bash", "git status && rm -rf x") == Decision.ask


def test_substitution_forbids_allow_match():
    e = engine(allow=["bash(echo *)"])
    # echo is a read-only builtin, but $() smuggling blocks the auto-allow
    assert e.evaluate("bash", "echo $(rm -rf /)") != Decision.allow


def test_plan_mode_blocks_mutation():
    e = engine(mode=Mode.plan)
    assert e.evaluate("edit", str(Path.cwd() / "a.py")) == Decision.deny
    assert e.evaluate("write", str(Path.cwd() / "a.py")) == Decision.deny
    # read-only builtins still allowed
    assert e.evaluate("bash", "ls") == Decision.allow


def test_yolo_allows_but_circuit_breaker_still_prompts():
    e = engine(mode=Mode.yolo)
    assert e.evaluate("bash", "npm install") == Decision.allow
    assert e.evaluate("bash", "rm -rf /") == Decision.ask


def test_auto_edit_allows_edits():
    e = engine(mode=Mode.auto_edit)
    assert e.evaluate("edit", str(Path.cwd() / "a.py")) == Decision.allow


def test_protected_path_always_prompts():
    e = engine(mode=Mode.auto_edit)
    assert e.evaluate("edit", str(Path.cwd() / ".git" / "config")) == Decision.ask


def test_dontask_auto_denies_unmatched():
    e = engine(mode=Mode.dontask)
    assert e.evaluate("bash", "npm install") == Decision.deny
    assert e.evaluate("bash", "ls") == Decision.allow  # readonly builtin still ok


def test_mode_cycle():
    assert next_mode(Mode.plan, allow_yolo=False) == Mode.ask
    assert next_mode(Mode.ask, allow_yolo=False) == Mode.auto_edit
    assert next_mode(Mode.auto_edit, allow_yolo=False) == Mode.plan
    assert next_mode(Mode.auto_edit, allow_yolo=True) == Mode.yolo
