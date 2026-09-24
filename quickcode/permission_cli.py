"""``quickcode permissions explain`` / ``qc why``: the permission dry run, in a terminal.

The answer ``POST /api/permissions/explain`` gives, for the project the command
is run in and a session opened there now -- the same engine, rules and profile
(``core/permission_posture``) and the same explanation
(``core/permission_explain``) -- printed as text. It is for writing rules: try
a command line against them without starting the app or spending a turn.

    qc why "git push origin main"
    qc why --mode auto-edit "npm test && rm -rf build"
    qc why --allow "bash(make **)" "make test"          # try a rule before writing it
    quickcode permissions explain --tool read .env
    quickcode permissions explain --tool edit --input '{"file_path": "src/a.py"}' --json

It sees what a new session sees. Tools from MCP servers exist only once the app
has connected to them, so they are not known here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

from quickcode.core.permissions import DEFAULT_SPEC, Mode


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Say whether a tool call would be allowed, prompted for or refused "
                    "in this project, and which check decided it. Nothing is run.",
    )
    parser.add_argument("target", nargs="?", default=None,
                        help="the command line for bash, or the tool's target (a path, a URL)")
    parser.add_argument("--tool", default="bash", help="the tool to ask about (default: bash)")
    parser.add_argument("--input", dest="input_json", default=None,
                        help="the call's arguments as a JSON object, instead of TARGET")
    parser.add_argument("--mode", default=None, choices=[m.value for m in Mode],
                        help="ask as if the session were in this mode "
                             "(default: the mode a new session starts in)")
    parser.add_argument("--cwd", default=None, help="project directory (default: current dir)")
    for kind in ("allow", "ask", "deny"):
        parser.add_argument(f"--{kind}", action="append", default=[], metavar="RULE",
                            help=f"add a {kind} rule for this question only (repeatable)")
    parser.add_argument("--no-project-rules", dest="project_rules", action="store_false",
                        help="leave out the rules in the project's settings files")
    parser.add_argument("--no-profile", dest="profile", action="store_false",
                        help="leave out the active permission profile")
    parser.add_argument("--json", action="store_true", help="print the full answer as JSON")
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def explain_here(cwd: Path, tool_name: str, *, input_json: str | None, target: str | None,
                 mode: str | None, rules: dict[str, list[str]] | None = None,
                 project_rules: bool = True, profile: bool = True) -> dict[str, Any]:
    """The explain payload for ``cwd``. Raises ``ValueError`` with the reason."""
    from quickcode.config import Config
    from quickcode.core import permission_explain, permission_posture
    from quickcode.plugins import loader
    from quickcode.security import trust
    from quickcode.tools.registry import default_registry

    config = Config.load()
    tools = [*default_registry().tools.values(), *loader.load_tool_plugins()]
    posture = permission_posture.for_new_session(
        cwd, config=config, tools=tools, allow_yolo=bool(config.allow_yolo),
        project_rules=project_rules, use_profile=profile,
    ).with_mode(Mode(mode) if mode else None).with_rules(
        permission_posture.what_if_rules(rules),
    )
    tool = posture.tools.get(tool_name)
    if tool is None:
        raise ValueError(f"no tool {tool_name!r} in this project; it has: "
                         f"{', '.join(sorted(posture.tools))}")
    call_input = None
    if input_json is not None:
        try:
            call_input = json.loads(input_json)
        except ValueError as exc:
            raise ValueError(f"--input is not valid JSON: {exc}") from None
    shell = getattr(tool, "permission", DEFAULT_SPEC).shell
    args = permission_explain.call_arguments(
        tool, input=call_input,
        command=target if shell else None,
        target=None if shell else target,
    )
    return permission_explain.explain(posture, tool_name, args,
                                      trusted=trust.resolve_trust(cwd))


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------

TAG = 8  # width of the "[allow] " column


def _wrap(text: str, indent: str, width: int, hang: int = 2) -> list[str]:
    return textwrap.wrap(text, width=width, initial_indent=indent,
                         subsequent_indent=indent + " " * hang) or [indent]


def _step_lines(step: dict[str, Any], indent: str, width: int) -> list[str]:
    tag = f"[{step['decision']}]" if step.get("decision") else "-"
    if step["step"] == "subcommand":
        out = [f"{indent}{tag:<{TAG}}{step['command']}"]
        for inner in step.get("steps") or []:
            out += _step_lines(inner, indent + " " * TAG, width)
        return out
    return _wrap(f"{tag:<{TAG}}{step.get('why', '')}", indent, width, hang=TAG)


def render(payload: dict[str, Any], *, width: int | None = None) -> str:
    width = width or min(shutil.get_terminal_size((100, 24)).columns, 100)
    posture = payload["posture"]
    source = {"request": "asked for", "session": "the session's",
              "new session": "a new session's"}.get(posture["mode_source"], "")
    context = [f"{posture['mode']} mode ({source})" if source else f"{posture['mode']} mode",
               f"profile {posture['profile']}" if posture["profile"] else "no profile",
               "project trusted" if posture["trusted"] else "project not trusted"]
    if not posture.get("project_rules", True):
        context.append("project rules left out")
    lines = [f"{payload['tool']}: {payload['target'] or '(no target)'}",
             "  " + " | ".join(context), ""]
    for bad in payload.get("invalid_rules") or []:
        lines += _wrap(f"Warning: the engine can never match the rule {bad!r}; "
                       "it was left out.", "", width)
    if payload.get("invalid_rules"):
        lines.append("")
    lines += _wrap(f"=> {payload['decision'].upper()}: {payload['summary']}", "", width)
    lines += ["", "How the gate got there:"]
    for step in payload["steps"]:
        lines += _step_lines(step, "  ", width)
    extra: list[str] = []
    if payload.get("suggestion"):
        extra += _wrap(payload["suggestion"]["text"], "", width)
    for hint in payload.get("hints") or []:
        extra += _wrap(f"Hint: {hint['text']}", "", width)
    for note in payload.get("notes") or []:
        extra += _wrap(f"Note: {note}", "", width)
    if extra:
        lines += ["", *extra]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """``argv`` starts at the subcommand: ``["why", ...]`` or
    ``["permissions", "explain", ...]``."""
    if argv[:1] == ["permissions"]:
        if argv[1:2] != ["explain"]:
            return _fail("usage: quickcode permissions explain [--tool TOOL] [--mode MODE] "
                         "TARGET  (or: qc why TARGET)")
        prog, rest = "quickcode permissions explain", argv[2:]
    else:
        prog, rest = "qc why", argv[1:]
    args = _parser(prog).parse_args(rest)
    if args.target is None and args.input_json is None:
        return _fail("say what to ask about: a command line, a target, or --input")

    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    if not cwd.is_dir():
        return _fail(f"not a directory: {cwd}")
    rules = {kind: getattr(args, kind) for kind in ("allow", "ask", "deny")}
    try:
        payload = explain_here(cwd.resolve(), args.tool, input_json=args.input_json,
                               target=args.target, mode=args.mode, rules=rules,
                               project_rules=args.project_rules, profile=args.profile)
    except ValueError as exc:
        return _fail(str(exc))

    from quickcode import headless

    headless.emit(json.dumps(payload, indent=2, ensure_ascii=False) if args.json
                  else render(payload))
    return 0
