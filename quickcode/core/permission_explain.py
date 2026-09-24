"""Why was I prompted? A dry run of the permission gate, explained.

The decision is always the real engine's: ``explain`` calls
``PermissionEngine.evaluate_tool(..., trace=[])``, the same call the agent loop
makes before every tool, and the trace it reads back is recorded by the lines
that decided (``permissions._traced``). What this module adds is only prose and
provenance around that trace:

* a sentence for each step, written here once so the CLI and the Help view
  print the same words instead of each keeping its own copy;
* which settings file or profile each matched rule came from -- the engine
  holds one merged list and cannot say;
* what "Always allow" would write (``PermissionEngine.suggest_rules``), and
  whether those rules would actually stop the next prompt (they do not for a
  protected path or a circuit breaker) -- answered by asking the engine again
  with the rules added, not by reasoning about it here;
* the things that change the answer without being part of the gate: allow
  rules an untrusted project's files state and the loader ignored, command
  hooks that may tighten it afterwards, and a tool the session never offers.

Nothing here executes anything. Hooks are counted, never run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode import jsonfile
from quickcode.core.permissions import (
    DEFAULT_SPEC,
    Decision,
    Mode,
    PermissionEngine,
    Rules,
)
from quickcode.security.protected import UNRESOLVABLE

# The two files ``Rules.load`` reads, in its order. Read again here only to say
# which of them a rule came from; the rules the engine runs are ``Rules.load``'s.
RULE_FILES = (".quickcode/settings.json", ".quickcode/settings.local.json")
# Where ``Rules.persist_allow`` writes an "Always allow".
ALWAYS_ALLOW_FILE = ".quickcode/settings.local.json"
KINDS = ("deny", "ask", "allow")


class UnknownTool(LookupError):
    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name = name
        self.known = known


@dataclass
class RuleSources:
    """Where each rule the engine holds was written."""

    by_rule: dict[tuple[str, str], list[dict[str, str]]] = field(default_factory=dict)
    # Allow rules a project's files state that the trust gate kept out.
    ignored_allow: list[dict[str, str]] = field(default_factory=list)
    # What an unattributed rule is: in a live session, an "always allow"
    # answered during it; in a fresh one, nothing should be unattributed.
    fallback: dict[str, str] = field(default_factory=dict)

    def of(self, kind: str, rule: str) -> list[dict[str, str]]:
        found = self.by_rule.get((kind, rule))
        if found:
            return found
        return [self.fallback] if self.fallback else []

    def add(self, kind: str, rule: str, scope: str, source: str) -> None:
        self.by_rule.setdefault((kind, rule), []).append({"scope": scope, "source": source})


def _permissions_block(path: Path) -> dict[str, Any]:
    try:
        data = jsonfile.load(path).get("permissions", {})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def rule_sources(cwd: Path, *, profile_id: str, trusted: bool, live: bool,
                 project: bool = True, extra: Any = None) -> RuleSources:
    from quickcode.core import profiles

    out = RuleSources()
    for rel in RULE_FILES if project else ():
        block = _permissions_block(cwd / rel)
        for kind in KINDS:
            rules = block.get(kind) or []
            for rule in rules if isinstance(rules, list) else []:
                if not isinstance(rule, str):
                    continue
                if kind == "allow" and not trusted:
                    out.ignored_allow.append({"rule": rule, "source": rel})
                    continue
                out.add(kind, rule, "project", rel)
    profile = profiles.load_profiles(cwd).get(profile_id) if profile_id else None
    if profile is not None:
        where = {"default": "built-in", "user": "your settings",
                 "project": "project settings"}.get(profile.layer, profile.layer)
        for kind in KINDS:
            for rule in getattr(profile, kind):
                out.add(kind, rule, "profile", f"{profile.id} ({where})")
    for kind in KINDS if extra is not None else ():
        for rule in getattr(extra, kind):
            out.add(kind, rule, "what-if", "added for this question")
    if live:
        out.fallback = {"scope": "session", "source": "approved during this session"}
    return out


# ---------------------------------------------------------------------------
# prose
# ---------------------------------------------------------------------------

_PROTECTED = ".git, .quickcode, .ssh, .env or .env.*, or outside the project"
_RULE_STEPS = ("deny_rule", "ask_rule", "allow_rule")
# Steps whose own ``steps`` hold a nested trace, and so a nested reason.
_NESTED = ("subcommand", "inner_command")


def _sources_text(sources: list[dict[str, str]]) -> str:
    if not sources:
        return ""
    return " (" + "; ".join(f"{s['scope']}: {s['source']}" for s in sources) + ")"


def _protected_what(step: dict[str, Any]) -> str:
    target = step.get("target", "")
    reason = step.get("reason", "argument")
    if reason == "command":
        return ("The command writes to or runs from a protected location (git config, "
                "for one, writes .git/config)")
    if reason == "cwd":
        return f"The shell stands in {target}, which is protected or outside the project"
    if reason == "sweep":
        return ("A recursive read would reach a protected file (.env, .git, .ssh) under "
                "the directories it names")
    if UNRESOLVABLE.search(target):
        return (f"{target} holds an expansion only the shell can finish, which counts "
                "as a protected path")
    return f"{target} is a protected path ({_PROTECTED})"


def _why(step: dict[str, Any], ctx: dict[str, Any]) -> str:
    name, decision = step["step"], step.get("decision")
    if name == "protected_path":
        what = _protected_what(step)
        if step.get("waived") == "yolo":
            return f"{what}, but yolo mode does not prompt for protected paths."
        if step.get("waived") == "artifact":
            return (f"{what}, but it is a subagent report under .quickcode/artifacts/, "
                    "which a read-only tool may read without the prompt.")
        if decision == "deny":
            return f"{what}; dontask never prompts, so it is refused."
        return (f"{what}, so it prompts. Only deny rules and plan mode are checked "
                "ahead of this, so no allow rule can answer it.")
    if name == "plan_mode":
        if ctx.get("shell"):
            return "Plan mode runs only the read-only builtins in the shell, and this is not one."
        return ("Plan mode refuses tools that change things; in a real session this "
                "tool is not even offered to the model.")
    if name in _RULE_STEPS:
        kind = name.split("_")[0]
        tail = {"deny": " Deny rules are checked first, so no allow rule can override it.",
                "ask": " Ask rules are checked before allow rules.",
                "allow": ""}[kind]
        return f"Matched the {kind} rule {step['rule']}{_sources_text(step['sources'])}.{tail}"
    if name == "read_only":
        return ("No rule matched, and the tool declares itself read-only, so it is "
                "allowed in every mode.")
    if name == "mode_default":
        return _mode_default_why(step, ctx)
    if name == "shell":
        n = step.get("subcommands", 1)
        text = ("One subcommand." if n == 1 else
                f"Split into {n} subcommands on && || | ; & and newlines; each is judged "
                "on its own and the most restrictive answer wins.")
        if step.get("substitution"):
            text += (" The line contains a substitution or redirection, which rules out "
                     "the read-only auto-allow and every allow rule.")
        return text
    if name == "parsed":
        bits = []
        if step.get("stripped") != step.get("command"):
            bits.append(f"matched as {step.get('stripped')!r} with wrappers and "
                        "environment assignments stripped")
        if step.get("env_prefix"):
            bits.append("it sets an environment variable, so neither the read-only "
                        "auto-allow nor an allow rule written without the assignment "
                        "applies")
        if step.get("opaque"):
            bits.append("it points the program at code no rule has seen (a -c setting, "
                        "an --exec-path), so allow rules do not cover it")
        text = "; ".join(bits)
        return text[:1].upper() + text[1:] + "."
    if name == "unresolvable_command":
        text = (f"The command word {step.get('name', '')!r} is only finished by the shell "
                "(a variable, a substitution or a glob), so it could be a command a deny "
                "rule forbids")
        return text + ("; dontask refuses it." if decision == "deny" else ", so it prompts.")
    if name == "readonly_builtin":
        if decision == "allow":
            return f"{step['name']} is a read-only builtin, so it runs without a prompt."
        why = [text for key, text in (
            ("substitution", "the line has a substitution or redirection"),
            ("env_prefix", "it sets an environment variable"),
            ("unsafe_option", "an option can make it run or write something"),
            ("local_program", "it names a file in the project, not the program on PATH"),
        ) if step.get(key)]
        return (f"{step['name']} is a read-only builtin, but {' and '.join(why) or 'it did not qualify'}"
                ", so it is not auto-allowed.")
    if name == "circuit_breaker":
        return ("The line matches a circuit breaker (a recursive delete of the root or a "
                "home directory, a forced git push, or a fork bomb), which prompts in "
                "every mode, yolo included.")
    if name == "nesting_limit":
        return "Commands nest deeper than the engine follows them, so it asks."
    if name in _NESTED:
        inner = _decided_by(step.get("steps") or [], decision or "")
        why = inner.get("why", "")
        return f"Run by the line above: {why}" if name == "inner_command" and why else why
    if name == "most_restrictive":
        if decision == Decision.allow.value:
            return "Every part of the line is allowed, and no circuit breaker matched."
        return f"Most restrictive answer across the line: {decision}."
    return ""


def _mode_default_why(step: dict[str, Any], ctx: dict[str, Any]) -> str:
    mode = step.get("mode", "")
    lead = "No rule matched"
    if ctx.get("substitution") or ctx.get("opaque"):
        lead = "No deny or ask rule matched (allow rules are not consulted for this command)"
    elif ctx.get("env_prefix"):
        lead = "No rule matched the command with its environment assignment"
    if mode == Mode.yolo.value:
        return f"{lead}; yolo allows it."
    if mode == Mode.dontask.value:
        return f"{lead}; dontask refuses rather than prompting."
    if step.get("shell"):
        extra = " (auto-edit allows edits, not commands)" if mode == Mode.auto_edit.value else ""
        return f"{lead}, so {mode} mode prompts for the command{extra}."
    if mode == Mode.auto_edit.value:
        if step.get("decision") == Decision.allow.value:
            return f"{lead}; auto-edit allows edits to files in the project."
        what = ("the tool runs a program" if step.get("executes")
                else "the tool's target is not a file")
        return f"{lead}; {what}, and auto-edit allows edits only, so it prompts."
    return f"{lead}, so {mode} mode prompts."


def _render_all(steps: list[dict[str, Any]], sources: RuleSources,
                ctx: dict[str, Any]) -> list[dict[str, Any]]:
    shell = next((s for s in steps if s["step"] == "shell"), None)
    if shell is not None:
        ctx = {**ctx, "shell": True, "substitution": shell.get("substitution")}
    return [r for s in steps if (r := _render(s, sources, ctx)) is not None]


def _render(step: dict[str, Any], sources: RuleSources,
            ctx: dict[str, Any]) -> dict[str, Any] | None:
    out = dict(step)
    kind = step["step"]
    if kind in _RULE_STEPS:
        out["sources"] = sources.of(kind.split("_")[0], step["rule"])
    elif kind == "subcommand":
        inner = step.get("steps") or []
        parsed = next((s for s in inner if s["step"] == "parsed"), {})
        sub_ctx = {**ctx, "env_prefix": parsed.get("env_prefix"),
                   "opaque": parsed.get("opaque")}
        out["steps"] = _render_all(
            [{**s, "command": step["command"]} if s["step"] == "parsed" else s for s in inner],
            sources, sub_ctx,
        )
    elif kind == "inner_command":
        out["steps"] = _render_all(step.get("steps") or [], sources, {"shell": True})
    elif kind == "parsed" and step.get("stripped") == step.get("command") and not (
        step.get("env_prefix") or step.get("opaque")
    ):
        return None  # nothing was stripped or disqualified; saying so is noise
    out["why"] = _why(out, ctx)
    return out


def _decided_by(steps: list[dict[str, Any]], final: str) -> dict[str, Any]:
    """The step that produced ``final`` -- inside a compound or nested line, the
    part of it that did, carrying that part's ``command``."""
    deciding = [s for s in steps if s.get("decision")]
    if not deciding:
        return {}
    last = deciding[-1]
    if last["step"] != "most_restrictive":
        return last
    parts = [s for s in deciding if s["step"] in _NESTED]
    if final == Decision.allow.value and len(parts) > 1:
        return last
    for s in deciding:
        if s["decision"] != final:
            continue
        if s["step"] in _NESTED:
            inner = _decided_by(s.get("steps") or [], final)
            if inner:
                return {**inner, "command": inner.get("command") or s["command"]}
            return s
        if s["step"] in ("circuit_breaker", "nesting_limit"):
            return s
    return last


# ---------------------------------------------------------------------------
# the dry run
# ---------------------------------------------------------------------------

def _engine_with(engine: PermissionEngine, **extra: list[str]) -> PermissionEngine:
    """The same engine with more rules -- a what-if, asked of the real code."""
    r = engine.rules
    rules = Rules(allow=list(r.allow) + extra.get("allow", []),
                  ask=list(r.ask), deny=list(r.deny))
    return PermissionEngine(mode=engine.mode, rules=rules, root=engine.root,
                            yolo_accepted=engine.yolo_accepted, specs=engine.specs)


def _spec_json(tool: Any) -> dict[str, Any]:
    spec = getattr(tool, "permission", DEFAULT_SPEC)
    return {"mutates": spec.mutates, "target_field": spec.target_field,
            "path_target": spec.path_target, "shell": spec.shell,
            "executes": spec.executes}


def call_arguments(tool: Any, *, input: Any = None, command: Any = None,  # noqa: A002
                   target: Any = None) -> dict[str, Any]:
    """One call's arguments, from whichever shape the caller had them in.

    ``input`` is the arguments object itself; ``command`` is the command line
    of a shell tool; ``target`` is the value of whatever field the tool
    declares as its target (a path, a URL). Raises ``ValueError`` with the
    reason for anything else.
    """
    spec = getattr(tool, "permission", DEFAULT_SPEC)
    given = [k for k, v in (("input", input), ("command", command), ("target", target))
             if v is not None]
    if len(given) > 1:
        raise ValueError(f"send one of input, command or target, not {' and '.join(given)}")
    if input is not None:
        if not isinstance(input, dict):
            raise ValueError("'input' must be a JSON object of the tool's arguments")
        return input
    if command is not None:
        if not spec.shell:
            raise ValueError(f"{tool.name!r} is not a shell tool; give its arguments or "
                             "its target instead of a command")
        if not isinstance(command, str):
            raise ValueError("'command' must be a string")
        return {spec.target_field or "command": command}
    if target is not None:
        if not isinstance(target, str):
            raise ValueError("'target' must be a string")
        return {spec.target_field: target} if spec.target_field else {}
    return {}


def explain(posture: Any, tool_name: str, args: dict[str, Any], *,
            trusted: bool) -> dict[str, Any]:
    """Ask the posture's engine about one call, and say why it answered so.

    ``posture`` is a ``permission_posture.Posture``. Raises ``UnknownTool``
    for a name no tool in the session carries.
    """
    tool = posture.tools.get(tool_name)
    if tool is None:
        raise UnknownTool(tool_name, sorted(posture.tools))
    engine: PermissionEngine = posture.engine
    cwd = Path(engine.root)

    trace: list[dict[str, Any]] = []
    shell_cwd = posture.shell_cwd
    decision, target = engine.evaluate_tool(tool, args, cwd=shell_cwd, trace=trace)

    sources = rule_sources(cwd, profile_id=posture.profile_id, trusted=trusted,
                           live=bool(posture.conv_id), project=posture.project_rules,
                           extra=posture.extra)
    spec = getattr(tool, "permission", DEFAULT_SPEC)
    steps = _render_all(trace, sources, {"shell": spec.shell})
    decided, summary = _summarize(steps, decision)

    notes: list[str] = []
    if tool.name not in posture.offered:
        notes.append("This session's composition does not give the agent this tool, so it "
                     "is never called; the answer is what the gate would say if it were.")
    elif engine.mode is Mode.plan and spec.mutates and not spec.shell:
        notes.append("In plan mode this tool is withheld from the model's tool list, so the "
                     "call cannot happen in the first place.")
    notes += _hook_notes(cwd, tool.name, trusted=trusted)

    hints: list[dict[str, Any]] = []
    if sources.ignored_allow:
        what_if = _engine_with(engine, allow=[i["rule"] for i in sources.ignored_allow])
        what_if_trace: list[dict[str, Any]] = []
        after, _ = what_if.evaluate_tool(tool, args, cwd=shell_cwd, trace=what_if_trace)
        if after is not decision:
            matched = _rules_hit(what_if_trace, "allow_rule")
            hints.append({
                "kind": "untrusted_allow",
                "decision": after.value,
                "rules": [i for i in sources.ignored_allow if i["rule"] in matched],
                "text": ("This project is not trusted, so the allow rules in its own "
                         "settings files are ignored"
                         + (f" ({', '.join(matched)})" if matched else "")
                         + f". Trusting it would make this call {after.value}."),
            })

    suggestion = None
    if decision is Decision.ask:
        offer = engine.suggest_rules(tool, args, cwd=shell_cwd)
        after_trace: list[dict[str, Any]] = []
        after, _ = _engine_with(engine, allow=list(offer.rules)).evaluate_tool(
            tool, args, cwd=shell_cwd, trace=after_trace)
        _, still = _summarize(_render_all(after_trace, sources, {"shell": spec.shell}), after)
        suggestion = {
            # The rules on one line, as the payload has always carried them.
            "rule": ", ".join(offer.rules),
            "rules": list(offer.rules),
            "kept": [{"part": part, "reason": reason} for part, reason in offer.kept],
            "file": ALWAYS_ALLOW_FILE,
            "persists": trusted,
            "next_time": after.value,
            "text": _suggestion_text(offer.rules, after, trusted, still),
        }

    return {
        "tool": tool.name,
        "target": target,
        "decision": decision.value,
        "summary": summary,
        "decided_by": decided,
        "steps": steps,
        "spec": _spec_json(tool),
        "offered": tool.name in posture.offered,
        "posture": {
            "mode": engine.mode.value,
            "mode_source": posture.mode_source,
            "profile": posture.profile_id,
            "trusted": trusted,
            "conv": posture.conv_id,
            "yolo_armed": posture.yolo_armed,
            "project_rules": posture.project_rules,
            "shell_cwd": str(shell_cwd) if shell_cwd is not None else None,
        },
        "suggestion": suggestion,
        "hints": hints,
        "notes": notes,
        # What-if lines the engine could never match, verbatim ("allow: x(").
        "invalid_rules": list(posture.extra.invalid) if posture.extra is not None else [],
    }


def _rules_hit(trace: list[dict[str, Any]], step: str) -> list[str]:
    out: list[str] = []
    for s in trace:
        if s["step"] == step and s.get("rule") not in out:
            out.append(s["rule"])
        out += [r for r in _rules_hit(s.get("steps") or [], step) if r not in out]
    return out


def _summarize(steps: list[dict[str, Any]],
               decision: Decision) -> tuple[dict[str, Any], str]:
    """The deciding step, and one sentence for it -- naming the part of the line
    that decided when there was more than one part."""
    decided = _decided_by(steps, decision.value)
    summary = decided.get("why", "")
    if decided.get("command") and sum(s["step"] in _NESTED for s in steps) > 1:
        summary = f"{decided['command']!r}: {summary}"
    return decided, summary


def _suggestion_text(rules: tuple[str, ...], after: Decision, trusted: bool,
                     still: str) -> str:
    if not rules:
        return f"Always allow would save nothing: no rule can stop this {after.value}. {still}"
    one = len(rules) == 1
    it = "it applies" if one else "they apply"
    where = (f"{ALWAYS_ALLOW_FILE}, and {it} to later sessions" if trusted else
             f"{ALWAYS_ALLOW_FILE}; the project is not trusted, so {it} for the "
             "rest of this session only")
    text = f"Always allow would write {', '.join(rules)} to {where}."
    if after is Decision.allow:
        return text + f" With {'it' if one else 'them'}, this exact call runs without asking next time."
    return text + (f" {'It' if one else 'They'} would not stop the next {after.value} "
                   f"for this call: {still}")


def _hook_notes(cwd: Path, tool_name: str, *, trusted: bool) -> list[str]:
    try:
        from quickcode.hooks.config import load_hooks

        hooks = load_hooks(cwd, trusted=trusted).matching("PreToolUse", tool_name)
    except Exception:
        return []
    if not hooks:
        return []
    n = len(hooks)
    return [f"{n} PreToolUse {'hook matches' if n == 1 else 'hooks match'} this tool. "
            "Hooks run after this gate and can only tighten its answer; a dry run "
            "does not execute them."]
