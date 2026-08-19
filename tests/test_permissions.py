from pathlib import Path

from quickcode.core.permissions import (
    Decision,
    Mode,
    PermissionEngine,
    Rules,
    _glob_match,
    next_mode,
)


def engine(mode=Mode.ask, root=None, **kw):
    return PermissionEngine(mode=mode, rules=Rules(**kw), root=root or Path.cwd())


def test_readonly_builtin_auto_allows():
    e = engine()
    assert e.evaluate("bash", "ls -la") == Decision.allow
    assert e.evaluate("bash", "cat file.txt") == Decision.allow


def test_find_is_not_auto_allowed_and_dangerous_find_prompts():
    e = engine()
    assert e.evaluate("bash", "find . -name '*.py'") == Decision.ask
    assert e.evaluate("bash", "find . -name '*.py' -exec rm {} ;") == Decision.ask
    assert e.evaluate("bash", "find . -delete") == Decision.ask


def test_bash_readonly_commands_prompt_for_protected_paths(tmp_path):
    e = engine(root=tmp_path)
    assert e.evaluate("bash", "cat .env") == Decision.ask
    assert e.evaluate("bash", "grep -r secret ~/.ssh") == Decision.ask
    assert e.evaluate("bash", "cat safe.txt") == Decision.allow


def test_single_star_does_not_cross_directory_boundaries():
    assert _glob_match("src/*", "src/file.py")
    assert not _glob_match("src/*", "src/deep/file.py")
    assert not _glob_match("src/*", r"src\deep\file.py")
    assert _glob_match("src/**", "src/deep/file.py")


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


def test_a_rule_can_name_an_mcp_tool_whose_server_has_a_hyphen():
    r"""`mcp__<server>__<tool>` is built from names we do not choose.

    The rule grammar used to spell the tool half as `\w+`, so a server called
    `company-kb` produced a tool no rule could ever name: the engine read
    `mcp__company-kb__kb_search(...)` as a bare tool name nothing is called,
    and it matched nothing without saying so.
    """
    name = "mcp__company-kb__kb_search"
    e = engine(mode=Mode.ask, allow=[f"{name}(*)"])
    assert e.evaluate(name, "anything") == Decision.allow

    denied = engine(mode=Mode.dontask, deny=[name])
    assert denied.evaluate(name, "anything") == Decision.deny


def test_a_rule_naming_a_dotted_tool_is_matched_not_dropped():
    e = engine(mode=Mode.ask, allow=["vendor.tool(src/**)"])
    assert e.evaluate("vendor.tool", "src/a.py") == Decision.allow
    assert e.evaluate("vendor.tool", "other/a.py") == Decision.ask
