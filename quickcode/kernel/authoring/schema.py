"""Per-kind key tables and the validator. Pure: no filesystem, no execution.

Validation runs twice, with the same code and different authority.

**On load it is authoritative.** Every registry build parses and validates every
authored file, and a file carrying an ``error`` is *skipped*: it produces no
spec, no tool, no section. It never raises. This is the rule ``preset.py``
already follows and the reason ``bootstrap._safe`` exists -- one malformed file
must not take down Settings, must not stop the app starting, and must not hide
the other plugins.

**On save it is advisory.** ``PUT .../source`` runs this same validator, writes
the file regardless, and returns the problems. Refusing to save half-finished
work would be hostile in an editor and pointless besides -- the file can be
edited in vim and the filesystem is the source of truth. What the save
guarantees is that you are told *now*, in the editor, rather than when you go
hunting for a tool that never loaded.

Every problem carries a ``fix``: the sentence that turns a rejection into a next
action. A validator that only says "no" is a validator people route around.
"""

from __future__ import annotations

import json
import re
from typing import Any

from quickcode.kernel.authoring import argv as argv_rules
from quickcode.kernel.authoring.format import Document, is_bool, parse_bool, parse_list
from quickcode.kernel.authoring.model import (
    CWD_MODES,
    OUTPUT_MODES,
    PARAM_TYPES,
    WHEN_VALUES,
    AuthoredPlugin,
    Param,
)
from quickcode.kernel.authoring.reserved import reserved_reason
from quickcode.kernel.problems import Problem, Provenance

# -- the authoring half of the error vocabulary ----------------------------
MISSING_KEY = "missing_key"
BAD_KIND = "bad_kind"
BAD_SLUG = "bad_slug"
ID_RESERVED = "id_reserved"
ID_DUPLICATE = "id_duplicate"
MISSING_BLOCK = "missing_block"
BAD_JSON = "bad_json"
UNKNOWN_PARAM_TYPE = "unknown_param_type"
UNKNOWN_PLACEHOLDER = "unknown_placeholder"
LIST_PLACEHOLDER_NOT_ALONE = "list_placeholder_not_alone"
BOOL_PLACEHOLDER_NOT_ALONE = "bool_placeholder_not_alone"
BAD_ENUM_CHOICE = "bad_enum_choice"
TIMEOUT_OUT_OF_RANGE = "timeout_out_of_range"
PATH_ESCAPES_PROJECT = "path_escapes_project"
UNKNOWN_AGENT_REF = "unknown_agent_ref"
ORDER_CONFLICT = "order_conflict"
SHELL_NOT_SUPPORTED = "shell_not_supported"
UNKNOWN_PERMISSION_TARGET = "unknown_permission_target"
READ_ONLY_UNVERIFIED = "read_only_unverified"
NEEDS_TRUST = "needs_trust"
NOT_DUPLICABLE = "not_duplicable"
SUBAGENT_SECTION_UNSUPPORTED = "subagent_section_unsupported"

KINDS = ("tool", "agent", "prompt")
# Named so the refusal can say what happened to them rather than "bad kind".
DEFERRED_KINDS = {
    "mcp": ("MCP servers are configured through the 'mcpServers' block in "
            "settings.json in this version"),
    "preset": ("presets live in settings.json in this version, beside "
               "'active_preset'"),
}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_MAX_TIMEOUT_MS = 600_000

# Commands that plainly change something. Used only to contradict a
# ``read_only: true`` claim out loud; never to allow or refuse anything.
_MUTATING_HINTS = frozenset({
    "rm", "mv", "cp", "install", "publish", "push", "commit", "deploy",
    "chmod", "chown", "kill", "format", "reset", "clean", "prune", "apply",
})


def _prov(scope: str, path: str, rule: str = "") -> Provenance:
    layer = "project" if scope == "project" else "user"
    return Provenance(layer=layer, source=path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                      path=path, rule=rule)


def _problem(code: str, severity: str, message: str, fix: str, *,
             subject: str, field: str, scope: str, path: str, line: int = 0) -> Problem:
    return Problem(
        code=code, severity=severity, message=message, fix=fix,  # type: ignore[arg-type]
        subject=subject, field=field, provenance=_prov(scope, path, field), line=line,
    )


def validate(
    doc: Document,
    *,
    scope: str,
    path: str = "",
    default_name: str = "",
    source_text: str = "",
) -> tuple[AuthoredPlugin | None, list[Problem]]:
    """Parse and check one document. Returns ``(plugin | None, problems)``.

    ``plugin`` is ``None`` exactly when a problem of severity ``error`` was
    found -- the two answers are one decision, so a caller cannot accidentally
    load something the validator rejected.
    """
    problems: list[Problem] = []
    meta = doc.meta
    name = (meta.get("name") or default_name or "").strip()
    kind = (meta.get("kind") or "").strip().lower()
    subject = f"{kind}.{name}" if kind and name else (name or path)

    def add(code, severity, message, fix, field="", line=0):
        problems.append(_problem(code, severity, message, fix, subject=subject,
                                 field=field, scope=scope, path=path,
                                 line=line or doc.line_of(field)))

    # -- kind --------------------------------------------------------------
    if not kind:
        add(MISSING_KEY, "error",
            "this file does not say what kind of plugin it is",
            "Add 'kind: tool', 'kind: agent' or 'kind: prompt' to the frontmatter.",
            field="kind")
        return None, problems
    if kind in DEFERRED_KINDS:
        add(BAD_KIND, "error",
            f"'kind: {kind}' is not authorable in this version -- "
            f"{DEFERRED_KINDS[kind]}",
            "Move the definition to settings.json, or change the kind.",
            field="kind")
        return None, problems
    if kind not in KINDS:
        add(BAD_KIND, "error",
            f"'{kind}' is not a plugin kind. Available: {', '.join(KINDS)}.",
            f"Change kind to one of: {', '.join(KINDS)}.", field="kind")
        return None, problems

    # -- name and id -------------------------------------------------------
    if not name:
        add(MISSING_KEY, "error", "this plugin has no name",
            "Add 'name: <slug>', or rename the file to the name you want.",
            field="name")
        return None, problems
    if not _SLUG_RE.match(name):
        add(BAD_SLUG, "error",
            f"'{name}' is not a valid name: lower-case letters, digits, "
            "'_' and '-', starting with a letter, at most 32 characters",
            "Rename the file, or set 'name:' to a valid slug.", field="name")
        return None, problems

    plugin_id = f"{kind}.{name}"
    reason = reserved_reason(plugin_id, kind, name)
    if reason:
        add(ID_RESERVED, "error",
            f"'{plugin_id}' cannot be used: {reason}",
            "Pick a different name. To start from the built-in one, use "
            "Duplicate instead -- shadowing an internal plugin is refused so a "
            "cloned repository cannot replace a tool you trust.",
            field="name")
        return None, problems

    common: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "scope": scope,
        "path": path,
        "title": meta.get("title", "").strip() or name,
        "description": meta.get("description", "").strip(),
        "group": meta.get("group", "").strip(),
        "enabled_by_default": parse_bool(meta.get("enabled_by_default", ""), True),
        "derived_from": meta.get("derived_from", "").strip(),
        "source_text": source_text,
        "prose": doc.prose,
    }

    if kind == "tool":
        plugin = _validate_tool(doc, common, add)
    elif kind == "agent":
        plugin = _validate_agent(doc, common, add)
    else:
        plugin = _validate_prompt(doc, common, add)

    if any(p.severity == "error" for p in problems):
        return None, problems
    return plugin, problems


# --------------------------------------------------------------------------
# kind: tool
# --------------------------------------------------------------------------

def _validate_tool(doc: Document, common: dict[str, Any], add) -> AuthoredPlugin | None:
    meta = doc.meta

    if not common["description"]:
        add(MISSING_KEY, "error",
            "a command tool needs a description -- it is what the model reads "
            "when deciding whether to call it",
            "Add a 'description:' line saying what the command does and when to "
            "use it.", field="description")

    if parse_bool(meta.get("shell", ""), False):
        add(SHELL_NOT_SUPPORTED, "error",
            "'shell: true' is not supported in this version: a command tool is "
            "argv-only, executed without a shell",
            "Write the command as a JSON argv array, one element per token. If "
            "you genuinely need a pipeline, use the bash tool.", field="shell")

    params, param_problems = _parse_params(doc, add)
    argv = _parse_argv(doc, add)

    by_name = {p.name: p for p in params}
    if argv and not param_problems:
        _check_placeholders(argv, by_name, add)

    timeout = _int(meta.get("timeout_ms", ""), 120_000)
    if timeout < 1 or timeout > _MAX_TIMEOUT_MS:
        add(TIMEOUT_OUT_OF_RANGE, "error",
            f"timeout_ms must be between 1 and {_MAX_TIMEOUT_MS}; this file says "
            f"{timeout}",
            f"Set timeout_ms to at most {_MAX_TIMEOUT_MS} (ten minutes), the "
            "same envelope the bash tool uses.", field="timeout_ms")
        timeout = min(max(timeout, 1), _MAX_TIMEOUT_MS)

    output = (meta.get("output", "") or "text").strip().lower()
    if output not in OUTPUT_MODES:
        add(BAD_ENUM_CHOICE, "error",
            f"output: '{output}' is not one of {', '.join(OUTPUT_MODES)}",
            f"Set output to one of: {', '.join(OUTPUT_MODES)}.", field="output")
        output = "text"

    on_nonzero = (meta.get("on_nonzero", "") or "error").strip().lower()
    if on_nonzero not in ("error", "content"):
        add(BAD_ENUM_CHOICE, "error",
            f"on_nonzero: '{on_nonzero}' is not 'error' or 'content'",
            "Use 'error' for a command whose failure is a failure, 'content' "
            "for one where a non-zero exit is the answer (a linter, a test run).",
            field="on_nonzero")
        on_nonzero = "error"

    cwd_mode = (meta.get("cwd", "") or "project").strip()
    if cwd_mode not in CWD_MODES:
        if cwd_mode.startswith(("/", "\\")) or ".." in cwd_mode.replace("\\", "/").split("/") \
                or re.match(r"^[A-Za-z]:", cwd_mode):
            add(PATH_ESCAPES_PROJECT, "error",
                f"cwd: '{cwd_mode}' leaves the project root",
                "Use 'project', 'file_dir', or a path relative to the project "
                "root that stays inside it.", field="cwd")
            cwd_mode = "project"

    max_output = _int(meta.get("max_output_chars", ""), 30_000)
    max_output = min(max(max_output, 200), 1_000_000)

    codes: list[int] = []
    for raw in parse_list(meta.get("success_exit_codes", "")) or ["0"]:
        try:
            codes.append(int(raw))
        except ValueError:
            add(BAD_ENUM_CHOICE, "warning",
                f"success_exit_codes contains '{raw}', which is not a number",
                "List plain integers, e.g. [0, 5].", field="success_exit_codes")
    if not codes:
        codes = [0]

    target = meta.get("permission_target", "").strip()
    if target and target not in by_name:
        add(UNKNOWN_PERMISSION_TARGET, "error",
            f"permission_target names '{target}', which is not a declared "
            f"parameter. Declared: {', '.join(sorted(by_name)) or 'none'}.",
            "Name one of the declared parameters, or remove the key.",
            field="permission_target")
        target = ""

    read_only = parse_bool(meta.get("read_only", ""), False)
    if read_only:
        add(READ_ONLY_UNVERIFIED, "info",
            "read_only is recorded but grants nothing: QuickCode cannot check "
            "what a command does, so this tool is still gated like any other "
            "mutating tool",
            "Nothing to do. To let it run without a prompt, add a permission "
            "rule naming it in settings.json -- that decision is yours to make "
            "and is visible where the other rules are.", field="read_only")
        first = argv[0] if argv else ""
        hits = sorted({t for t in argv if t.lstrip("-") in _MUTATING_HINTS})
        if hits:
            add(READ_ONLY_UNVERIFIED, "warning",
                f"this tool declares read_only but its command contains "
                f"{', '.join(repr(h) for h in hits)}",
                f"Either drop 'read_only: true' or check what '{first}' does "
                "with those arguments.", field="read_only")

    env_from = tuple(parse_list(meta.get("env_from", "")))
    env_literal = _json_block(doc, "env", add, default={})
    if not isinstance(env_literal, dict):
        env_literal = {}

    stdin_block = doc.blocks.get("stdin")

    return AuthoredPlugin(
        **common,
        params=tuple(params),
        argv=tuple(argv),
        label=meta.get("label", "").strip(),
        cwd_mode=cwd_mode,
        timeout_ms=timeout,
        output=output,
        max_output_chars=max_output,
        success_exit_codes=tuple(codes),
        on_nonzero=on_nonzero,
        read_only_declared=read_only,
        permission_target=target,
        env_from=env_from,
        env_literal={str(k): str(v) for k, v in env_literal.items()},
        stdin=stdin_block.text if stdin_block else "",
    )


def _parse_params(doc: Document, add) -> tuple[list[Param], bool]:
    block = doc.blocks.get("params")
    if block is None:
        add(MISSING_BLOCK, "error",
            "a command tool needs a ```json params block, even an empty one",
            "Add:\n\n```json params\n[]\n```", field="params")
        return [], True
    raw = _load_json(block.text, "params", block.line, add)
    if raw is None:
        return [], True
    if not isinstance(raw, list):
        add(BAD_JSON, "error", "the params block must be a JSON array",
            "Wrap the parameters in [ ... ], one object per parameter.",
            field="params", line=block.line)
        return [], True

    out: list[Param] = []
    seen: set[str] = set()
    failed = False
    for entry in raw:
        if not isinstance(entry, dict):
            add(BAD_JSON, "error", "each parameter must be a JSON object",
                'Use {"name": "path", "type": "string"}.',
                field="params", line=block.line)
            failed = True
            continue
        name = str(entry.get("name", "")).strip()
        if not _PARAM_NAME_RE.match(name):
            add(BAD_SLUG, "error",
                f"'{name}' is not a valid parameter name: lower-case letters, "
                "digits and '_', starting with a letter",
                "Rename the parameter. It becomes a field in the schema the "
                "model is handed, so it has to be an identifier.",
                field="params", line=block.line)
            failed = True
            continue
        if name in seen:
            add(ID_DUPLICATE, "error",
                f"two parameters are both called '{name}'",
                "Rename one of them.", field="params", line=block.line)
            failed = True
            continue
        seen.add(name)
        ptype = str(entry.get("type", "string")).strip().lower()
        if ptype not in PARAM_TYPES:
            add(UNKNOWN_PARAM_TYPE, "error",
                f"'{name}' has type '{ptype}'. Available: {', '.join(PARAM_TYPES)}.",
                f"Change the type to one of: {', '.join(PARAM_TYPES)}.",
                field="params", line=block.line)
            failed = True
            continue
        choices = tuple(str(c) for c in entry.get("choices", []) or ())
        if ptype == "enum" and not choices:
            add(BAD_ENUM_CHOICE, "error",
                f"enum parameter '{name}' declares no choices",
                'Add "choices": ["a", "b"] so the model knows what it may send.',
                field="params", line=block.line)
            failed = True
            continue
        item_type = str(entry.get("item_type", "string")).strip().lower()
        if ptype == "list" and item_type not in ("string", "int", "float", "path"):
            add(UNKNOWN_PARAM_TYPE, "error",
                f"list parameter '{name}' has item_type '{item_type}'",
                "Use item_type string, int, float or path.",
                field="params", line=block.line)
            failed = True
            continue
        out.append(Param(
            name=name,
            type=ptype,
            description=str(entry.get("description", "")),
            required=bool(entry.get("required", False)),
            default=entry.get("default"),
            choices=choices,
            item_type=item_type,
            pattern=str(entry.get("pattern", "")),
            minimum=_opt_float(entry.get("minimum")),
            maximum=_opt_float(entry.get("maximum")),
            max_length=_opt_int(entry.get("max_length")),
            flag=str(entry.get("flag", "")),
        ))
    return out, failed


def _parse_argv(doc: Document, add) -> list[str]:
    block = doc.blocks.get("argv")
    if block is None:
        add(MISSING_BLOCK, "error",
            "a command tool needs a ```json argv block: the command as an "
            "array, one element per argument",
            'Add:\n\n```json argv\n["git", "status", "--short"]\n```',
            field="argv")
        return []
    raw = _load_json(block.text, "argv", block.line, add)
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        add(BAD_JSON, "error",
            "the argv block must be a non-empty JSON array of strings",
            'One element per argument: ["uv", "run", "pytest", "-q"]. The array '
            "is executed directly, never through a shell, which is why a value "
            "can never become two arguments.", field="argv", line=block.line)
        return []
    if not raw[0].strip() or argv_rules.placeholders(raw[0]):
        add(BAD_JSON, "error",
            "the first argv element is the program to run and must be a "
            "literal name or path",
            "Put the program in element 0 and the parameters after it.",
            field="argv", line=block.line)
        return []
    return list(raw)


def _check_placeholders(argv: list[str], params: dict[str, Param], add) -> None:
    declared = ", ".join(sorted(params)) or "none"
    for element in argv:
        names = argv_rules.placeholders(element)
        whole = argv_rules.whole_placeholder(element)
        for name in names:
            if name not in params:
                add(UNKNOWN_PLACEHOLDER, "error",
                    f"argv references {{{name}}}, which is not a declared "
                    f"parameter. Declared: {declared}.",
                    f"Rename it to one of {declared}, or add a parameter called "
                    f"'{name}'. An undeclared placeholder is refused rather "
                    "than substituted empty, because a command that silently "
                    "loses an argument still runs.", field="argv")
                continue
            param = params[name]
            if param.type == "list" and whole != name:
                add(LIST_PLACEHOLDER_NOT_ALONE, "error",
                    f"list parameter '{name}' appears inside '{element}'",
                    f'Give it an element of its own: "{{{name}}}". A list '
                    "expands to one argument per item, which only means "
                    "something when nothing else shares the element.",
                    field="argv")
            if param.type == "bool" and whole != name:
                add(BOOL_PLACEHOLDER_NOT_ALONE, "error",
                    f"boolean parameter '{name}' appears inside '{element}'",
                    f'Give it an element of its own: "{{{name}}}". True emits '
                    f'its flag (--{name.replace("_", "-")} unless you set '
                    '"flag"); false drops the element.', field="argv")


# --------------------------------------------------------------------------
# kind: agent
# --------------------------------------------------------------------------

def _validate_agent(doc: Document, common: dict[str, Any], add) -> AuthoredPlugin | None:
    meta = doc.meta
    if not common["description"]:
        add(MISSING_KEY, "error",
            "an agent needs a description -- the spawning agent reads it to "
            "decide what to delegate here",
            "Add a 'description:' line saying what this agent is for and what "
            "it must not be given.", field="description")
    if not common["prose"].strip():
        add(MISSING_KEY, "error",
            "an agent's body is its system prompt, and this file has none",
            "Write the agent's instructions below the closing '---'.",
            field="body")

    cap = meta.get("mode_cap", "").strip()
    if cap:
        from quickcode.core.permissions import Mode
        try:
            Mode(cap)
        except ValueError:
            choices = ", ".join(m.value for m in Mode)
            add(BAD_ENUM_CHOICE, "error",
                f"mode_cap: '{cap}' is not a permission mode. Available: {choices}.",
                f"Use one of: {choices}.", field="mode_cap")

    if meta.get("role", "").strip().lower() == "orchestrator":
        add(BAD_ENUM_CHOICE, "warning",
            "an authored file cannot declare itself the orchestrator; this "
            "loads as an ordinary subagent",
            "Remove the 'role:' line. The orchestrator is the reserved id "
            "'@orchestrator' and nothing else can claim it.", field="role")

    turns = meta.get("max_turns", "").strip()
    if turns and not turns.lstrip("-").isdigit():
        add(BAD_ENUM_CHOICE, "warning",
            f"max_turns: '{turns}' is not a number; the default of 30 applies",
            "Set max_turns to a whole number of delegation turns.",
            field="max_turns")

    return AuthoredPlugin(**common, agent_meta=dict(meta))


# --------------------------------------------------------------------------
# kind: prompt
# --------------------------------------------------------------------------

def _validate_prompt(doc: Document, common: dict[str, Any], add) -> AuthoredPlugin | None:
    meta = doc.meta
    if not common["prose"].strip():
        add(MISSING_KEY, "error",
            "a prompt section with no body would contribute nothing",
            "Write the section text below the closing '---'. Wrapping it in an "
            "XML-ish tag the way the internal sections do is recommended.",
            field="body")

    after = meta.get("after", "").strip()
    order = 0
    if after:
        target = _section_order(after)
        if target is None:
            add(UNKNOWN_AGENT_REF, "warning",
                f"after: '{after}' names no prompt section on this machine; "
                "this section falls to the end of the prompt",
                "Check the id, or use 'order:' with a number instead.",
                field="after")
            order = 200
        else:
            order = target + 1
    elif meta.get("order", "").strip():
        order = _int(meta.get("order", ""), 200)
    else:
        order = 200

    if _section_id_at(order) is not None:
        add(ORDER_CONFLICT, "warning",
            f"order {order} is already taken by '{_section_id_at(order)}'; ties "
            "break by id, so the composed prompt stays deterministic",
            "Nothing to do, or pick a different order if you meant to come "
            "first.", field="order")

    applies_to = tuple(parse_list(meta.get("applies_to", "")) or ["main"])
    for entry in applies_to:
        if entry not in ("main", "subagents") and not entry.startswith("agent:"):
            add(BAD_ENUM_CHOICE, "error",
                f"applies_to: '{entry}' is not 'main', 'subagents' or "
                "'agent:<name>'",
                "Use applies_to: [main] for the agent you talk to.",
                field="applies_to")
    if any(e == "subagents" or e.startswith("agent:") for e in applies_to):
        add(SUBAGENT_SECTION_UNSUPPORTED, "info",
            "subagent prompts are rendered from their own template in this "
            "version, so this section reaches the main agent only",
            "Put the same text in the agent's own definition body if a "
            "subagent needs it.", field="applies_to")

    when = (meta.get("when", "") or "always").strip().lower()
    if when not in WHEN_VALUES:
        add(BAD_ENUM_CHOICE, "error",
            f"when: '{when}' is not one of {', '.join(WHEN_VALUES)}",
            f"Use one of: {', '.join(WHEN_VALUES)}.", field="when")
        when = "always"

    return AuthoredPlugin(
        **common, order=order, after=after, applies_to=applies_to, when=when,
    )


def _section_order(section_id: str) -> int | None:
    try:
        from quickcode.prompts import sections as prompt_sections

        section = prompt_sections.get(section_id)
    except Exception:
        return None
    return None if section is None else section.order


def _section_id_at(order: int) -> str | None:
    try:
        from quickcode.prompts import sections as prompt_sections

        for section in prompt_sections.SECTIONS:
            if section.order == order:
                return section.id
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _load_json(text: str, field: str, line: int, add) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        add(BAD_JSON, "error",
            f"the {field} block is not valid JSON: {exc}",
            "Fix the JSON. Trailing commas and single quotes are the usual "
            "two.", field=field, line=line)
        return None


def _json_block(doc: Document, tag: str, add, *, default: Any) -> Any:
    block = doc.blocks.get(tag)
    if block is None:
        return default
    value = _load_json(block.text, tag, block.line, add)
    return default if value is None else value


def _int(raw: str, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _opt_int(raw: Any) -> int | None:
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _opt_float(raw: Any) -> float | None:
    try:
        return None if raw is None else float(raw)
    except (TypeError, ValueError):
        return None


__all__ = ["validate", "KINDS", "DEFERRED_KINDS", "is_bool"]
