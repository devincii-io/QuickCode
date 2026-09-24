"""The engine's trace hook: the explanation comes from the deciding line.

``evaluate(..., trace=[])`` must answer exactly what ``evaluate(...)`` answers
-- the trace is a by-product of the one code path, not a second opinion -- and
the step it records last must be the one that decided.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quickcode.core.permissions import Decision, Mode, PermissionEngine, Rules
from quickcode.tools.registry import default_registry

CASES = [
    # (tool, target, rules)
    ("bash", "ls -la", {}),
    ("bash", "git push origin main", {"allow": ["bash(git **)"], "deny": ["bash(git push**)"]}),
    ("bash", "npm test && rm -rf build", {"allow": ["bash(npm *)"]}),
    ("bash", "echo $(whoami)", {"allow": ["bash(echo *)"]}),
    ("bash", "PATH=. ls", {}),
    ("bash", "cat .env", {}),
    ("bash", "rm -rf /", {}),
    ("bash", "git push -f origin main", {"allow": ["bash(git **)"]}),
    ("bash", "/bin/cat secrets.txt", {"deny": ["bash(cat **)"]}),
    ("bash", "git status", {"ask": ["bash(git status)"], "allow": ["bash(git **)"]}),
    ("bash", "xargs rm -rf", {"deny": ["bash(rm **)"], "allow": ["bash(xargs **)"]}),
    ("bash", "$CMD -rf build", {"deny": ["bash(rm **)"]}),
    ("bash", "bash -c 'git push --force'", {}),
    ("bash", "grep -r KEY .", {}),
    ("read", "src/app.py", {}),
    ("read", ".env", {"allow": ["read(**)"]}),
    ("read", "notes.pem", {"deny": ["read(**.pem)"]}),
    ("edit", "src/app.py", {"allow": ["edit(src/**)"]}),
    ("edit", "src/app.py", {}),
    ("write", ".git/config", {"allow": ["write"]}),
    ("web_fetch", "https://example.com", {"deny": ["web_fetch"]}),
]


def _engine(root: Path, mode: Mode, rules: dict) -> PermissionEngine:
    return PermissionEngine(mode=mode, rules=Rules(**rules), root=root)


def _last_deciding(trace: list[dict]) -> dict:
    return [s for s in trace if s["decision"] is not None][-1]


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize(("tool", "target", "rules"), CASES)
def test_a_traced_evaluation_answers_exactly_what_an_untraced_one_does(
    tmp_path, mode, tool, target, rules,
):
    engine = _engine(tmp_path, mode, rules)
    trace: list[dict] = []
    traced = engine.evaluate(tool, target, trace=trace)

    assert traced == engine.evaluate(tool, target)
    assert trace, "every evaluation records at least the step that decided"
    assert _last_deciding(trace)["decision"] == traced.value


def test_evaluate_tool_threads_the_trace_through_the_tool_object(tmp_path):
    engine = _engine(tmp_path, Mode.ask, {"deny": ["read(**.pem)"]})
    tool = default_registry().get("read")
    trace: list[dict] = []

    decision, target = engine.evaluate_tool(tool, {"file_path": "k.pem"}, trace=trace)

    assert (decision, target) == engine.evaluate_tool(tool, {"file_path": "k.pem"})
    assert trace[-1] == {"step": "deny_rule", "decision": "deny", "rule": "read(**.pem)"}


def test_a_compound_line_is_traced_per_subcommand(tmp_path):
    engine = _engine(tmp_path, Mode.auto_edit, {"allow": ["bash(npm *)"]})
    trace: list[dict] = []

    assert engine.evaluate("bash", "npm test && rm -rf build", trace=trace) is Decision.ask

    subs = [s for s in trace if s["step"] == "subcommand"]
    assert [(s["command"], s["decision"]) for s in subs] == [
        ("npm test", "allow"), ("rm -rf build", "ask"),
    ]
    assert subs[0]["steps"][-1] == {"step": "allow_rule", "decision": "allow", "rule": "bash(npm *)"}
    assert subs[1]["steps"][-1]["step"] == "mode_default"
    assert trace[-1] == {"step": "most_restrictive", "decision": "ask"}


def test_a_circuit_breaker_is_named_even_in_yolo(tmp_path):
    engine = _engine(tmp_path, Mode.yolo, {})
    trace: list[dict] = []

    assert engine.evaluate("bash", "rm -rf /", trace=trace) is Decision.ask

    breaker = [s for s in trace if s["step"] == "circuit_breaker"]
    assert breaker == [{"step": "circuit_breaker", "decision": "ask"}]


def test_a_command_another_command_runs_is_traced_as_its_own_line(tmp_path):
    engine = _engine(tmp_path, Mode.ask, {"deny": ["bash(rm **)"], "allow": ["bash(xargs **)"]})
    trace: list[dict] = []

    assert engine.evaluate("bash", "xargs rm -rf", trace=trace) is Decision.deny

    inner = [s for s in trace if s["step"] == "inner_command"]
    assert inner and inner[0]["decision"] == "deny"
    assert any(s["step"] == "deny_rule" and s["rule"] == "bash(rm **)"
               for sub in inner[0]["steps"] if sub["step"] == "subcommand"
               for s in sub["steps"])


def test_a_shell_protected_path_names_the_word_that_named_it(tmp_path):
    engine = _engine(tmp_path, Mode.ask, {})
    trace: list[dict] = []

    assert engine.evaluate("bash", "cat notes.txt ~/.ssh/id_rsa", trace=trace) is Decision.ask

    steps = next(s for s in trace if s["step"] == "subcommand")["steps"]
    assert steps[-1] == {"step": "protected_path", "decision": "ask",
                         "reason": "argument", "target": "~/.ssh/id_rsa"}


def test_a_waived_protected_path_is_recorded_without_deciding(tmp_path):
    engine = _engine(tmp_path, Mode.yolo, {})
    trace: list[dict] = []

    assert engine.evaluate("edit", ".env", trace=trace) is Decision.allow
    assert trace[0] == {"step": "protected_path", "decision": None,
                        "target": ".env", "waived": "yolo"}


def test_a_disqualified_builtin_says_why(tmp_path):
    engine = _engine(tmp_path, Mode.ask, {"allow": ["bash(echo *)"]})
    trace: list[dict] = []

    assert engine.evaluate("bash", "echo hi > out.txt", trace=trace) is Decision.ask

    steps = next(s for s in trace if s["step"] == "subcommand")["steps"]
    builtin = next(s for s in steps if s["step"] == "readonly_builtin")
    assert builtin["decision"] is None and builtin["substitution"] is True
