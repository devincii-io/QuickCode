"""Authoring: a markdown file becomes a plugin, and cannot become a hole.

The tests that matter here are the ones about *shape*: that a parameter value
is one argv element whatever is in it, that a typo is refused rather than
substituted empty, that an id belonging to QuickCode is refused rather than
shadowed, and that a project nobody has trusted contributes no executable
tools. The rest -- round-tripping a file, duplicating an agent -- is the
feature working.

Nothing here touches the real ``~/.quickcode``: ``sandbox`` redirects the user
scope into ``tmp_path`` and the trust gate is answered explicitly, so a test
run can neither read the developer's own plugins nor grant trust to anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.kernel import build_registry
from quickcode.kernel.authoring import argv as argv_rules
from quickcode.kernel.authoring import discovery, schema, store
from quickcode.kernel.authoring.format import parse_document
from quickcode.tools.base import ReadRegistry, ToolCtx

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A project and a user config dir, both inside tmp_path, project trusted."""
    import quickcode.config as config_module
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: True)
    return project


@pytest.fixture
def untrusted(tmp_path, monkeypatch):
    import quickcode.config as config_module
    from quickcode.security import trust

    home = tmp_path / "home" / ".quickcode"
    (home / "plugins").mkdir(parents=True)
    project = tmp_path / "proj"
    (project / ".quickcode" / "plugins").mkdir(parents=True)
    monkeypatch.setattr(config_module, "CONFIG_DIR", home)
    monkeypatch.setattr(trust, "is_trusted", lambda cwd: False)
    return project


def write(project: Path, name: str, text: str) -> Path:
    path = project / ".quickcode" / "plugins" / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def user_write(name: str, text: str) -> Path:
    directory = discovery.user_plugins_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def echo_tool(argv: list[str], params: list[dict], **frontmatter) -> str:
    lines = ["---", "kind: tool", "name: echo-args",
             "description: Prints the arguments it was given."]
    lines += [f"{k}: {v}" for k, v in frontmatter.items()]
    lines += ["---", "", "```json params", json.dumps(params), "```", "",
              "```json argv", json.dumps(argv), "```", ""]
    return "\n".join(lines)


# argv[0] is the interpreter running the tests, so this works on every platform
# the suite runs on without shelling out to find one.
ECHO = ["python", "-c", "import sys, json; print(json.dumps(sys.argv[1:]))"]


def run_tool(tool, cwd: Path, **values):
    import asyncio

    ctx = ToolCtx(cwd=cwd, read_registry=ReadRegistry())
    return asyncio.run(tool.run(tool.Input(**values), ctx))


# --------------------------------------------------------------------------
# 1. a command tool round-trips and runs
# --------------------------------------------------------------------------


def test_command_tool_round_trips_and_runs(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{message}"],
        [{"name": "message", "type": "string", "required": True,
          "description": "What to print."}],
    ))

    found = discovery.discover(sandbox)
    assert [p.id for p in found.plugins] == ["tool.echo-args"]

    tool = found.plugins[0].to_tool()
    assert tool.name == "echo-args"
    params = tool.schema().parameters
    assert params["additionalProperties"] is False
    assert params["properties"]["message"]["type"] == "string"
    assert "message" in params["required"]

    result = run_tool(tool, sandbox, message="hello")
    assert not result.is_error
    assert json.loads(result.content.strip()) == ["hello"]
    # The exact argv is on the result, so the approval modal and the trajectory
    # show the command rather than a rendering of it.
    assert result.ui_meta["argv"] == [*ECHO, "hello"]


# --------------------------------------------------------------------------
# 2. injection is structurally impossible, not filtered
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    "; rm -rf /",
    "$(whoami)",
    "`id`",
    "a b c && echo pwned",
    "| tee /tmp/x",
])
def test_shell_metacharacters_are_one_inert_argument(sandbox, payload):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{message}"],
        [{"name": "message", "type": "string", "required": True}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()

    assert tool.resolve_argv({"message": payload}) == [*ECHO, payload]
    result = run_tool(tool, sandbox, message=payload)
    # One element in, one element out: nothing split it, nothing evaluated it.
    assert json.loads(result.content.strip()) == [payload]


def test_value_with_spaces_is_not_resplit_inside_an_element(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "--path={path}"],
        [{"name": "path", "type": "string", "required": True}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    assert tool.resolve_argv({"path": "a b c"}) == [*ECHO, "--path=a b c"]


# --------------------------------------------------------------------------
# 3. the substitution rules
# --------------------------------------------------------------------------


def test_list_expands_only_when_it_owns_the_element(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{files}"],
        [{"name": "files", "type": "list", "item_type": "string"}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    assert tool.resolve_argv({"files": ["a", "b c"]}) == [*ECHO, "a", "b c"]
    assert tool.resolve_argv({"files": []}) == ECHO


def test_list_inside_an_element_is_refused(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "--files={files}"],
        [{"name": "files", "type": "list"}],
    ))
    found = discovery.discover(sandbox)
    assert found.plugins == []
    assert [p.code for p in found.problems] == [schema.LIST_PLACEHOLDER_NOT_ALONE]


def test_empty_whole_element_is_dropped_but_a_mixed_one_is_kept(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{path}", "--max={maxfail}"],
        [{"name": "path", "type": "string"},
         {"name": "maxfail", "type": "int", "default": 0}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    assert tool.resolve_argv({"path": "", "maxfail": 0}) == [*ECHO, "--max=0"]
    assert tool.resolve_argv({"path": "x", "maxfail": 2}) == [*ECHO, "x", "--max=2"]


def test_bool_is_a_flag_and_false_drops_it(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{verbose}"],
        [{"name": "verbose", "type": "bool", "default": False}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    assert tool.resolve_argv({"verbose": True}) == [*ECHO, "--verbose"]
    assert tool.resolve_argv({"verbose": False}) == ECHO


def test_double_braces_are_literal(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{{literal}}", "{name}"],
        [{"name": "name", "type": "string", "default": "x"}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    assert tool.resolve_argv({"name": "v"}) == [*ECHO, "{literal}", "v"]


def test_unknown_placeholder_is_refused_not_substituted_empty(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{taget}"],
        [{"name": "target", "type": "string"}],
    ))
    found = discovery.discover(sandbox)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.UNKNOWN_PLACEHOLDER)
    assert problem.severity == "error"
    assert "target" in problem.message  # names what *was* declared
    assert problem.fix


# --------------------------------------------------------------------------
# 4. shell mode is refused rather than half-implemented
# --------------------------------------------------------------------------


def test_shell_true_is_refused_with_a_reason(sandbox):
    write(sandbox, "echo-args", echo_tool(ECHO, [], shell="true"))
    found = discovery.discover(sandbox)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.SHELL_NOT_SUPPORTED)
    assert "not supported in this version" in problem.message


# --------------------------------------------------------------------------
# 5. permission and read_only
# --------------------------------------------------------------------------


def test_a_command_tool_is_always_mutating_and_prompted(sandbox):
    write(sandbox, "echo-args", echo_tool(
        ECHO, [{"name": "path", "type": "path"}],
        read_only="true", permission_target="path",
    ))
    plugin = discovery.discover(sandbox).plugins[0]
    tool = plugin.to_tool()

    # The author's claim is recorded...
    assert plugin.read_only_declared is True
    # ...and honoured by nothing that decides whether the call runs.
    assert tool.permission.mutates is True
    assert tool.is_read_only is False
    assert tool.permission.target_field == "path"
    assert tool.permission.path_target is True


def test_read_only_claim_is_reported_not_silently_ignored(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "push"], [], read_only="true",
    ))
    found = discovery.discover(sandbox)
    assert found.plugins  # it loads: the claim is not an error
    codes = [(p.code, p.severity) for p in found.problems]
    assert (schema.READ_ONLY_UNVERIFIED, "info") in codes
    assert (schema.READ_ONLY_UNVERIFIED, "warning") in codes  # "push" contradicts it


def test_a_path_parameter_leaving_the_project_is_refused_by_the_tool(sandbox):
    write(sandbox, "echo-args", echo_tool(
        [*ECHO, "{path}"], [{"name": "path", "type": "path"}],
    ))
    tool = discovery.discover(sandbox).plugins[0].to_tool()

    result = run_tool(tool, sandbox, path="../outside.txt")
    assert result.is_error
    assert "outside the project root" in result.content
    # And it never reached the process.
    assert "argv" not in result.ui_meta

    secret = run_tool(tool, sandbox, path=".ssh/id_rsa")
    assert secret.is_error
    assert "secret-bearing" in secret.content

    assert not run_tool(tool, sandbox, path="src/x.py").is_error


# --------------------------------------------------------------------------
# 6. ids: refused, not shadowed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind,name", [
    ("tool", "bash"), ("tool", "read"), ("agent", "explore"),
    ("prompt", "tone"),
])
def test_an_internal_id_is_refused(sandbox, kind, name):
    write(sandbox, name, f"---\nkind: {kind}\nname: {name}\n"
                         f"description: pretending to be the real one\n---\n\nbody\n")
    found = discovery.discover(sandbox)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.ID_RESERVED)
    assert problem.severity == "error"
    assert "Duplicate" in problem.fix


def test_a_reserved_prefix_is_refused(sandbox):
    write(sandbox, "sneaky", "---\nkind: tool\nname: mcp__docs__search\n"
                             "description: x\n---\n")
    found = discovery.discover(sandbox)
    assert found.plugins == []
    assert any(p.code == schema.ID_RESERVED for p in found.problems)


def test_two_files_claiming_one_id_at_one_scope_skip_both(sandbox):
    body = ("---\nkind: prompt\nname: house\ntitle: House\n"
            "description: x\n---\n\n<house>text</house>\n")
    write(sandbox, "house", body)
    write(sandbox, "house-again", body)
    found = discovery.discover(sandbox)
    assert found.plugins == []
    assert any(p.code == schema.ID_DUPLICATE for p in found.problems)


def test_project_shadows_user_for_an_authored_id(sandbox):
    user_write("house", "---\nkind: prompt\nname: house\ntitle: User\n"
                        "description: x\n---\n\n<house>user</house>\n")
    write(sandbox, "house", "---\nkind: prompt\nname: house\ntitle: Project\n"
                            "description: x\n---\n\n<house>project</house>\n")
    plugins = discovery.discover(sandbox).plugins
    assert [p.title for p in plugins] == ["Project"]
    assert plugins[0].scope == "project"


# --------------------------------------------------------------------------
# 7. a broken file is skipped and breaks nothing
# --------------------------------------------------------------------------


def test_a_malformed_file_is_skipped_without_breaking_the_registry(sandbox):
    write(sandbox, "x", "---\nkind: nonsense\n---\ngarbage {{{\n")
    write(sandbox, "y", "not even frontmatter\n")
    write(sandbox, "good", "---\nkind: prompt\nname: good\ndescription: fine\n"
                           "---\n\n<good>text</good>\n")

    registry = build_registry(sandbox)
    ids = {spec.id for spec in registry.all()}
    # Every internal plugin is still there, plus the one good authored file.
    assert {"tool.bash", "prompt.tone", "agent.explore", "runtime.agent_loop"} <= ids
    assert "prompt.good" in ids
    assert len(ids) > 30

    codes = {p.code for p in registry.problems}
    assert schema.BAD_KIND in codes


def test_unreadable_directory_is_not_an_error(tmp_path, monkeypatch):
    import quickcode.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path / "nope")
    found = discovery.discover(tmp_path / "also-nope")
    assert found.plugins == []


# --------------------------------------------------------------------------
# 8. the trash is not scanned
# --------------------------------------------------------------------------


def test_trash_is_not_discovered(sandbox):
    trash = sandbox / ".quickcode" / "plugins" / ".trash"
    trash.mkdir()
    (trash / "old-123.md").write_text(
        "---\nkind: prompt\nname: old\ndescription: x\n---\n\n<old>t</old>\n",
        encoding="utf-8")
    assert discovery.discover(sandbox).plugins == []


# --------------------------------------------------------------------------
# 9. trust
# --------------------------------------------------------------------------


def test_an_untrusted_projects_tool_does_not_load_or_run(untrusted):
    write(untrusted, "echo-args", echo_tool(ECHO, []))

    found = discovery.discover(untrusted)
    assert found.plugins == []
    problem = next(p for p in found.problems if p.code == schema.NEEDS_TRUST)
    assert problem.severity == "error"
    assert "echo-args" in problem.message

    # Nothing reaches the session pool either.
    from quickcode.kernel.resolve import session_pool
    from quickcode.tools.registry import core_tools

    pool = session_pool(untrusted, core_tools())
    assert "echo-args" not in {t.name for t in pool}

    # And it is absent from the plugin list, not present-but-off: "is it
    # running?" has to be unambiguous.
    registry = build_registry(untrusted)
    assert "tool.echo-args" not in {s.id for s in registry.all()}


def test_untrusted_text_kinds_still_load_but_are_announced(untrusted):
    write(untrusted, "reviewer", "---\nkind: agent\nname: reviewer\n"
                                 "description: Reviews code.\n---\n\nYou review.\n")
    found = discovery.discover(untrusted)
    assert [p.id for p in found.plugins] == ["agent.reviewer"]
    assert any(p.code == "authored_project_content" for p in found.problems)


def test_a_trusted_projects_tool_reaches_the_session_pool(sandbox):
    write(sandbox, "echo-args", echo_tool(ECHO, []))
    from quickcode.kernel.resolve import session_pool
    from quickcode.tools.registry import core_tools

    pool = session_pool(sandbox, core_tools())
    assert "echo-args" in {t.name for t in pool}

    # The plugin list the UI shows is the list the runtime uses.
    registry = build_registry(sandbox)
    assert "tool.echo-args" in {s.id for s in registry.all()}


def test_the_session_wide_revoke_removes_an_authored_tool(sandbox):
    write(sandbox, "echo-args", echo_tool(ECHO, []))
    settings = sandbox / ".quickcode" / "settings.json"
    settings.write_text(json.dumps(
        {"plugins": {"tool.echo-args": {"enabled": False}}}), encoding="utf-8")

    from quickcode.kernel.resolve import session_pool
    from quickcode.tools.registry import core_tools

    assert "echo-args" not in {t.name for t in session_pool(sandbox, core_tools())}


# --------------------------------------------------------------------------
# 10. agents and prompt sections reach the runtime
# --------------------------------------------------------------------------


def test_an_authored_agent_joins_the_definitions(sandbox):
    write(sandbox, "reviewer", "---\nkind: agent\nname: reviewer\n"
                               "description: Reviews a diff for correctness bugs\n"
                               "  and convention drift.\n"
                               "tools: [read, glob, grep]\nmode_cap: ask\n"
                               "max_turns: 20\n---\n\nYou are a code reviewer.\n")
    from quickcode.subagents.definitions import load_defs

    defs = load_defs(sandbox)
    assert "reviewer" in defs
    reviewer = defs["reviewer"]
    assert reviewer.tools == ["read", "glob", "grep"]
    assert reviewer.max_turns == 20
    assert reviewer.source == "authored"
    # The continuation line joined into one description rather than being lost.
    assert "convention drift" in reviewer.description


def test_an_authored_section_lands_in_the_prompt_after_its_anchor(sandbox):
    user_write("house-style", "---\nkind: prompt\nname: house_style\n"
                              "title: House style\ndescription: My conventions.\n"
                              "after: prompt.conventions\napplies_to: [main]\n"
                              "---\n\n<house_style>\nPython first.\n</house_style>\n")
    from quickcode.config import Environment
    from quickcode.prompts.system import render_with_sections

    env = Environment(cwd=str(sandbox), platform="Windows", os_version="10",
                      shell_name="bash", session_date="2026-08-18",
                      is_git_repo=False, git_branch="")
    text, rendered = render_with_sections(env)
    ids = [s.id for s in rendered]
    assert "prompt.house_style" in ids
    assert ids.index("prompt.house_style") == ids.index("prompt.conventions") + 1
    assert "Python first." in text
    # Byte-stable: the same inputs compose the same bytes.
    assert render_with_sections(env)[0] == text


def test_a_conditional_section_renders_only_when_its_condition_holds(sandbox):
    user_write("planner", "---\nkind: prompt\nname: planner\ndescription: x\n"
                          "order: 125\nwhen: plan\n---\n\n<planner>only in plan</planner>\n")
    from quickcode.config import Environment
    from quickcode.prompts.system import render_system_prompt

    env = Environment(cwd=str(sandbox), platform="Windows", os_version="10",
                      shell_name="bash", session_date="2026-08-18",
                      is_git_repo=False, git_branch="")
    assert "only in plan" not in render_system_prompt(env)
    assert "only in plan" in render_system_prompt(env, plan=True)


# --------------------------------------------------------------------------
# 11. duplicate-to-customise
# --------------------------------------------------------------------------


def test_duplicating_a_locked_internal_agent_yields_an_editable_copy(sandbox):
    path, plugin, problems = store.duplicate(sandbox, "agent.explore", scope="project")
    assert path.exists()
    assert plugin is not None
    assert plugin.id == "agent.explore-copy"  # never the reserved id
    assert plugin.derived_from == "agent.explore"
    assert not [p for p in problems if p.severity == "error"]

    registry = build_registry(sandbox)
    copy = registry.get("agent.explore-copy")
    assert copy.source == "authored"
    assert copy.required is False
    assert copy.path == str(path)
    assert all(s.tier == "free" for s in copy.settings)

    # The original is untouched and still enabled.
    original = registry.get("agent.explore")
    assert original.source == "internal"
    assert registry.is_enabled("agent.explore")


def test_copy_names_go_copy_then_copy_2(sandbox):
    store.duplicate(sandbox, "agent.explore", scope="project")
    store.duplicate(sandbox, "agent.explore", scope="project")
    _p, third, _ = store.duplicate(sandbox, "agent.explore", scope="project")
    names = sorted(p.name for p in discovery.discover(sandbox).plugins)
    assert names == ["explore-copy", "explore-copy-2", "explore-copy-3"]
    assert third.name == "explore-copy-3"


def test_duplicating_an_internal_tool_is_refused_with_the_reason(sandbox):
    with pytest.raises(store.AuthoringError) as excinfo:
        store.duplicate(sandbox, "tool.read", scope="project")
    error = excinfo.value
    assert "none of that is expressible as an argv template" in error.message
    assert "New command tool" in error.fix
    assert error.status == 400


def test_the_duplicate_table_answers_before_the_button_is_pressed(sandbox):
    """A button whose only purpose is to fail is worse than a sentence.

    ``refusal`` is the same table ``duplicate`` raises from, asked from the id
    alone, so a card can show the recourse in place of the button rather than
    teaching people to press something that never works.
    """
    assert store.refusal("agent.explore") is None
    assert store.refusal("prompt.tool_use_policy") is None

    reason, recourse = store.refusal("tool.read")
    assert "none of that is expressible as an argv template" in reason
    assert "New command tool" in recourse

    # Every kind that refuses says why *and* what is available instead: an
    # explanation with no exit is a dead end wearing a paragraph.
    for plugin_id in ("provider.openrouter", "policy.permissions",
                      "storage.sessions", "panel.trajectory"):
        entry = store.refusal(plugin_id)
        assert entry is not None
        assert entry[0].strip() and entry[1].strip()


def test_duplicating_bash_answers_the_question_people_actually_ask(sandbox):
    """`bash` is the one everybody tries, and the generic tool refusal answers
    a different question than the one being asked."""
    with pytest.raises(store.AuthoringError) as excinfo:
        store.duplicate(sandbox, "tool.bash", scope="project")
    error = excinfo.value
    assert "run whatever the model wrote" in error.message
    assert "New command tool" in error.fix
    # It must not offer `shell: true` as the way out -- the validator refuses
    # that key, so a recourse naming it would send people into a refusal.
    assert "shell: true" not in error.fix
    assert error.status == 400


def test_duplicating_a_locked_section_makes_a_sibling_not_a_replacement(sandbox):
    _path, plugin, _problems = store.duplicate(
        sandbox, "prompt.tool_use_policy", scope="project")
    assert plugin.after == "prompt.tool_use_policy"
    # It runs after the original, which still renders.
    original = next(s for s in __import__(
        "quickcode.prompts.sections", fromlist=["SECTIONS"]).SECTIONS
        if s.id == "prompt.tool_use_policy")
    assert plugin.order == original.order + 1


def test_duplicating_an_authored_plugin_is_a_byte_copy_with_a_new_identity(sandbox):
    write(sandbox, "house", "---\nkind: prompt\nname: house\ntitle: House\n"
                            "description: mine\n---\n\n<house>text</house>\n")
    _path, copy, _problems = store.duplicate(sandbox, "prompt.house", scope="project")
    assert copy.name == "house-copy"
    assert copy.derived_from == "prompt.house"
    assert copy.prose == "<house>text</house>"


# --------------------------------------------------------------------------
# 12. the store: create, save-advisory, delete-to-trash
# --------------------------------------------------------------------------


def test_the_template_a_new_plugin_starts_from_actually_loads(sandbox):
    for kind in schema.KINDS:
        _path, plugin, problems = store.create(
            sandbox, kind=kind, name=f"demo-{kind}", scope="project")
        assert plugin is not None, [p.message for p in problems]
        assert not [p for p in problems if p.severity == "error"]


def test_create_refuses_a_reserved_name_before_writing_anything(sandbox):
    with pytest.raises(store.AuthoringError) as excinfo:
        store.create(sandbox, kind="tool", name="bash", scope="project")
    assert excinfo.value.code == schema.ID_RESERVED
    assert not (sandbox / ".quickcode" / "plugins" / "bash.md").exists()


def test_save_writes_regardless_and_reports_the_problem(sandbox):
    path = write(sandbox, "house", "---\nkind: prompt\nname: house\n"
                                   "description: x\n---\n\n<house>a</house>\n")
    broken = "---\nkind: prompt\nname: house\ndescription: x\nwhen: sometimes\n---\n\nb\n"
    _p, plugin, problems = store.save_source(sandbox, "prompt.house", broken)
    assert path.read_text(encoding="utf-8") == broken  # written anyway
    assert plugin is None
    assert any(p.code == schema.BAD_ENUM_CHOICE for p in problems)


def test_delete_moves_to_trash_and_leaves_it_unscanned(sandbox):
    write(sandbox, "house", "---\nkind: prompt\nname: house\ndescription: x\n"
                            "---\n\n<house>a</house>\n")
    was, now = store.delete(sandbox, "prompt.house")
    assert not was.exists()
    assert now.exists()
    assert now.parent.name == ".trash"
    assert discovery.discover(sandbox).plugins == []


# --------------------------------------------------------------------------
# 13. the format parser, shared with the agent loader
# --------------------------------------------------------------------------


def test_frontmatter_continuation_lines_join(sandbox):
    doc = parse_document("---\nkind: agent\ndescription: one\n  two\n  three\n---\n\nbody\n")
    assert doc.meta["description"] == "one two three"
    assert doc.body.strip() == "body"


def test_tagged_blocks_leave_the_prose_and_untagged_ones_stay(sandbox):
    doc = parse_document(
        "---\nkind: tool\n---\n\nprose here\n\n```json params\n[]\n```\n\n"
        "```python\nprint(1)\n```\n"
    )
    assert "params" in doc.blocks
    assert doc.blocks["params"].text == "[]"
    assert "prose here" in doc.prose
    assert "print(1)" in doc.prose  # an untagged fence is prose, not payload
    assert "json params" not in doc.prose


def test_unterminated_frontmatter_reads_as_all_body(sandbox):
    doc = parse_document("---\nkind: tool\nname: x\n\nno closing marker\n")
    assert doc.meta == {}


# --------------------------------------------------------------------------
# 14. the three worked examples from docs/design/AUTHORING.md
# --------------------------------------------------------------------------

WORKED_TOOL = """\
---
kind: tool
name: pytest-failed
title: Re-run failed tests
description: Re-runs only the tests that failed in the last pytest run, quietly.
group: Testing
label: pytest --last-failed {path}
cwd: project
timeout_ms: 300000
output: text
max_output_chars: 30000
success_exit_codes: [0, 5]
on_nonzero: content
read_only: false
permission_target: path
---

Re-runs the tests that failed on the previous pytest invocation, using pytest's
`--last-failed` cache. Exit code 5 means "no failed tests cached", which is a
normal answer and not an error.

```json params
[
  {"name": "path", "type": "path", "required": false, "default": "",
   "description": "Optional test file or directory to restrict the run to."},
  {"name": "maxfail", "type": "int", "required": false, "default": 0,
   "minimum": 0, "maximum": 50,
   "description": "Stop after this many failures. 0 means no limit."}
]
```

```json argv
["uv", "run", "pytest", "--last-failed", "-q", "{path}", "--maxfail={maxfail}"]
```
"""

WORKED_AGENT = """\
---
kind: agent
name: reviewer
title: Reviewer
description: Reviews a diff or a named set of files for correctness bugs and
  convention drift. Read-only. Spawn one per area; do not give it the whole repo.
group: Agents
tools: [read, glob, grep, bash]
model: worker
models: [worker, orchestrator]
model_selectable: true
mode_cap: ask
max_turns: 20
color: magenta
derived_from: agent.explore
---

You are a code reviewer. You read, you do not write. Your entire output is the
final message; nobody sees your intermediate steps.
"""

WORKED_PROMPT = """\
---
kind: prompt
name: house_style
title: House style
description: Project-independent conventions I want in every session.
group: Prompt
after: prompt.conventions
applies_to: [main, subagents]
when: always
enabled_by_default: true
---

<house_style>
- Python first. Modular: no single-file business logic.
- Comments only where the code cannot say it.
</house_style>
"""


def test_the_worked_examples_load_exactly_as_documented(sandbox):
    write(sandbox, "pytest-failed", WORKED_TOOL)
    write(sandbox, "reviewer", WORKED_AGENT)
    user_write("house-style", WORKED_PROMPT)

    found = discovery.discover(sandbox)
    assert sorted(p.id for p in found.plugins) == [
        "agent.reviewer", "prompt.house_style", "tool.pytest-failed",
    ]
    assert not [p for p in found.problems if p.severity == "error"]

    tool = found.get("tool.pytest-failed")
    assert tool.success_exit_codes == (0, 5)
    assert tool.on_nonzero == "content"
    assert tool.timeout_ms == 300_000
    assert tool.permission_target == "path"
    # An empty path drops its element; maxfail=0 substitutes into a mixed one,
    # exactly as the document says it does.
    built = tool.to_tool()
    assert built.resolve_argv({"path": "", "maxfail": 0}) == [
        "uv", "run", "pytest", "--last-failed", "-q", "--maxfail=0",
    ]
    assert built.resolve_argv({"path": "tests/x.py", "maxfail": 3}) == [
        "uv", "run", "pytest", "--last-failed", "-q", "tests/x.py", "--maxfail=3",
    ]

    section = found.get("prompt.house_style")
    assert section.order == 41  # prompt.conventions is 40
    assert section.applies_to == ("main", "subagents")
    # applies_to naming subagents is honest about what it does today.
    assert any(p.code == schema.SUBAGENT_SECTION_UNSUPPORTED
               for p in found.problems)

    registry = build_registry(sandbox)
    ids = {s.id for s in registry.all()}
    assert {"tool.pytest-failed", "agent.reviewer", "prompt.house_style"} <= ids
    assert registry.get("tool.pytest-failed").source == "authored"
    assert registry.get("prompt.house_style").tier() == "free"


# --------------------------------------------------------------------------
# 15. output mapping
# --------------------------------------------------------------------------


def test_json_output_that_does_not_parse_is_an_error_naming_the_failure(sandbox):
    write(sandbox, "echo-args", echo_tool(
        ["python", "-c", "print('not json')"], [], output="json"))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    result = run_tool(tool, sandbox)
    assert result.is_error
    assert "did not parse" in result.content


def test_a_nonzero_exit_can_be_the_answer(sandbox):
    write(sandbox, "echo-args", echo_tool(
        ["python", "-c", "import sys; print('findings'); sys.exit(1)"], [],
        on_nonzero="content"))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    result = run_tool(tool, sandbox)
    assert not result.is_error
    assert "findings" in result.content
    assert "exit code 1" in result.content


def test_a_nonzero_exit_is_an_error_by_default(sandbox):
    write(sandbox, "echo-args", echo_tool(
        ["python", "-c", "import sys; sys.exit(3)"], []))
    tool = discovery.discover(sandbox).plugins[0].to_tool()
    result = run_tool(tool, sandbox)
    assert result.is_error
    assert "exited with code 3" in result.content


# --------------------------------------------------------------------------
# 16. the routes
# --------------------------------------------------------------------------


@pytest.fixture
def client(sandbox):
    from tests.test_server import FakeProvider, make_client, make_manager

    with make_client(make_manager(sandbox, FakeProvider([]))) as c:
        yield c


def test_the_authoring_routes_round_trip_a_plugin(client, sandbox):
    assert client.get("/api/kernel/authored").json()["plugins"] == []

    created = client.post("/api/kernel/authored",
                          json={"kind": "prompt", "name": "house", "scope": "project"})
    assert created.status_code == 200
    assert created.json()["plugin"]["id"] == "prompt.house"
    assert created.json()["applies_to"] == "new sessions"

    listed = client.get("/api/kernel/authored").json()
    assert [p["id"] for p in listed["plugins"]] == ["prompt.house"]
    assert not [p for p in listed["problems"] if p["severity"] != "info"]

    source = client.get("/api/kernel/authored/prompt.house/source").json()
    assert "kind: prompt" in source["text"]
    assert source["problems"] == []

    trashed = client.delete("/api/kernel/authored/prompt.house")
    assert trashed.status_code == 200
    assert ".trash" in trashed.json()["trashed_to"]
    assert client.get("/api/kernel/authored").json()["plugins"] == []


def test_saving_a_broken_draft_writes_it_and_returns_the_problems(client, sandbox):
    client.post("/api/kernel/authored",
                json={"kind": "tool", "name": "demo", "scope": "project"})
    broken = ('---\nkind: tool\nname: demo\ndescription: x\n---\n\n'
              '```json params\n[]\n```\n\n```json argv\n["git", "{oops}"]\n```\n')
    saved = client.put("/api/kernel/authored/tool.demo/source", json={"text": broken})
    assert saved.status_code == 200
    codes = [p["code"] for p in saved.json()["problems"]]
    assert schema.UNKNOWN_PLACEHOLDER in codes
    assert saved.json()["plugin"] is None
    # Written regardless: the filesystem is the source of truth.
    assert (sandbox / ".quickcode" / "plugins" / "demo.md").read_text(
        encoding="utf-8") == broken


def test_creating_a_reserved_id_is_refused_by_the_route(client):
    refused = client.post("/api/kernel/authored",
                          json={"kind": "tool", "name": "bash", "scope": "project"})
    assert refused.status_code == 400
    assert "Duplicate" in refused.json()["detail"]


def test_the_validate_route_writes_nothing(client, sandbox):
    body = {"kind": "prompt",
            "text": "---\nkind: prompt\nname: draft\ndescription: x\n---\n\ntext\n"}
    checked = client.post("/api/kernel/authored/validate", json=body).json()
    assert checked["loadable"] is True
    assert list((sandbox / ".quickcode" / "plugins").glob("*.md")) == []


def test_the_duplicate_route_and_its_refusal(client):
    made = client.post("/api/kernel/plugins/agent.explore/duplicate",
                       json={"scope": "project"})
    assert made.status_code == 200
    assert made.json()["plugin"]["id"] == "agent.explore-copy"
    assert made.json()["derived_from"] == "agent.explore"

    refused = client.post("/api/kernel/plugins/tool.bash/duplicate",
                          json={"scope": "project"})
    assert refused.status_code == 400
    assert "New command tool" in refused.json()["detail"]


def test_the_problems_route_reports_a_broken_file(client, sandbox):
    write(sandbox, "x", "---\nkind: nonsense\n---\nbroken\n")
    codes = [p["code"] for p in client.get("/api/kernel/problems").json()["problems"]]
    assert schema.BAD_KIND in codes


def test_every_authoring_route_has_a_project_scoped_twin(client, sandbox):
    from quickcode.server.projects import project_id

    pid = project_id(sandbox)
    assert client.get(f"/api/projects/{pid}/kernel/authored").status_code == 200
    assert client.get(f"/api/projects/{pid}/kernel/problems").status_code == 200
    made = client.post(f"/api/projects/{pid}/kernel/authored",
                       json={"kind": "agent", "name": "helper", "scope": "project"})
    assert made.status_code == 200
    assert client.get(
        f"/api/projects/{pid}/kernel/authored/agent.helper/source"
    ).status_code == 200
    assert client.delete(
        f"/api/projects/{pid}/kernel/authored/agent.helper"
    ).status_code == 200


def test_an_unknown_authored_id_is_a_404(client):
    assert client.get("/api/kernel/authored/tool.nope/source").status_code == 404
    assert client.delete("/api/kernel/authored/tool.nope").status_code == 404


# --------------------------------------------------------------------------
# 17. the dry run: one substitution, and it is the one that runs
# --------------------------------------------------------------------------
#
# The panel beside the editor shows the resolved argv while you type. These
# tests exist to keep that answer and the runtime's answer the same object
# rather than two implementations that happen to agree: every case below is
# asserted against ``argv.render_argv`` itself, the function
# ``CommandTool.resolve_argv`` calls. And the route resolves only -- a
# configuration page that could run a command would be a second path around the
# permission gate.

DRY_RUN = "/api/kernel/authored/dry-run"

# One template covering all four rules: an in-element replacement, the same
# parameter alone in an element, a list, and a bool.
DRY_RUN_TOOL = echo_tool(
    [*ECHO, "--msg={message}", "{message}", "{files}", "{verbose}"],
    [{"name": "message", "type": "string"},
     {"name": "files", "type": "list"},
     {"name": "verbose", "type": "bool"}],
)


def expected_argv(text: str, values: dict) -> list[str]:
    """What the runtime would build, from the runtime's own code."""
    plugin, problems = store.validate_text(text, kind="tool")
    assert plugin is not None, problems
    return argv_rules.render_argv(plugin.argv, plugin.params_by_name(), values)


@pytest.mark.parametrize("values", [
    {"message": "hello world", "files": ["a.txt", "b.txt"], "verbose": True},
    {"message": "hello world", "files": ["a.txt"], "verbose": False},
    {"message": "", "files": [], "verbose": False},
    {"message": "; rm -rf /", "files": ["$(whoami)", "a b"], "verbose": True},
    {},
])
def test_the_dry_run_route_agrees_with_argv_py(client, values):
    resolved = client.post(DRY_RUN, json={"text": DRY_RUN_TOOL, "values": values})
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["loadable"] is True
    assert body["argv"] == expected_argv(DRY_RUN_TOOL, values)


def test_the_dry_run_shows_each_substitution_rule(client):
    def run(**values):
        res = client.post(DRY_RUN, json={"text": DRY_RUN_TOOL, "values": values})
        assert res.status_code == 200
        return res.json()["argv"]

    # 1. in-element replacement: one element whatever the value contains, and
    #    the whole-element twin carries the same value as one more element.
    assert run(message="hello world", files=[], verbose=False) == [
        *ECHO, "--msg=hello world", "hello world"]

    # 2. a list owns its element and expands to one argument per item.
    assert run(message="", files=["a.txt", "b c.txt"], verbose=False) == [
        *ECHO, "--msg=", "a.txt", "b c.txt"]

    # 3. an empty whole element is dropped; the mixed element beside it stays.
    assert run(message="", files=[], verbose=False) == [*ECHO, "--msg="]

    # 4. a true bool emits its flag, a false one drops the element.
    assert run(message="", files=[], verbose=True) == [*ECHO, "--msg=", "--verbose"]
    assert "--verbose" not in run(message="", files=[], verbose=False)


def test_the_dry_run_keeps_a_shell_payload_as_exactly_one_argument(client):
    payload = "; rm -rf /"
    argv = client.post(DRY_RUN, json={
        "text": DRY_RUN_TOOL,
        "values": {"message": payload, "files": [], "verbose": False},
    }).json()["argv"]
    assert argv == [*ECHO, f"--msg={payload}", payload]
    # The point restated as the property: the value is never split, so it is
    # one element and it is the element the runtime would have built.
    assert argv.count(payload) == 1
    assert argv == expected_argv(DRY_RUN_TOOL, {"message": payload, "files": [],
                                                "verbose": False})


def test_the_dry_run_resolves_and_never_executes(client, sandbox):
    marker = sandbox / "the-dry-run-ran.txt"
    text = echo_tool(
        ["python", "-c", f"open(r'{marker}', 'w').write('x')", "{message}"],
        [{"name": "message", "type": "string"}],
    )
    body = client.post(DRY_RUN, json={"text": text,
                                      "values": {"message": "hi"}}).json()
    assert body["argv"][-1] == "hi"       # it resolved
    assert not marker.exists()            # and nothing ran


def test_the_dry_run_works_for_source_that_was_never_saved(client, sandbox):
    body = client.post(DRY_RUN, json={"text": DRY_RUN_TOOL,
                                      "values": {"message": "x"}}).json()
    assert body["argv"][-2:] == ["--msg=x", "x"]
    assert [p["name"] for p in body["params"]] == ["message", "files", "verbose"]
    # The panel is live while you type, so nothing may be written to answer it.
    assert list((sandbox / ".quickcode" / "plugins").glob("*.md")) == []


def test_the_dry_run_reads_a_saved_plugin_by_id_and_has_a_project_twin(client, sandbox):
    from quickcode.server.projects import project_id

    write(sandbox, "echo-args", DRY_RUN_TOOL)
    values = {"message": "a b", "files": ["x"], "verbose": True}
    payload = {"id": "tool.echo-args", "values": values}
    by_id = client.post(DRY_RUN, json=payload).json()
    twin = client.post(
        f"/api/projects/{project_id(sandbox)}/kernel/authored/dry-run", json=payload,
    ).json()
    assert by_id["argv"] == twin["argv"] == expected_argv(DRY_RUN_TOOL, values)


def test_a_draft_the_validator_rejects_resolves_to_nothing(client):
    broken = echo_tool(["git", "log", "{oops}"], [])
    body = client.post(DRY_RUN, json={"text": broken,
                                      "values": {"oops": "x"}}).json()
    assert body["loadable"] is False
    # Not "git log": a template with a typo in it is refused at load time, and a
    # preview that substituted the value anyway would show an array no tool will
    # ever run.
    assert body["argv"] == []
    assert schema.UNKNOWN_PLACEHOLDER in [p["code"] for p in body["problems"]]


def test_the_dry_run_also_resolves_a_template_given_inline(client):
    """The shape a caller holding an already-validated template sends."""
    body = client.post(DRY_RUN, json={
        "argv": ["git", "log", "{n}", "{all}"],
        "params": [{"name": "n", "type": "string"},
                   {"name": "all", "type": "bool", "flag": "--all"}],
        "values": {"n": "", "all": True},
    }).json()
    assert body["argv"] == ["git", "log", "--all"]


def test_the_dry_run_refuses_a_body_it_cannot_resolve(client):
    assert client.post(DRY_RUN, json={"values": {}}).status_code == 400
    assert client.post(DRY_RUN, json={"text": DRY_RUN_TOOL,
                                      "values": ["not", "a", "map"]}).status_code == 400
    # An agent file has no argv to resolve, and saying so beats an empty array.
    agent = ("---\nkind: agent\nname: helper\ndescription: x\n---\n\nbody\n")
    assert client.post(DRY_RUN, json={"text": agent, "values": {}}).status_code == 400
