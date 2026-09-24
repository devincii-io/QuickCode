"""The tool half of an agent's view: the picker's rows, the schemas, the footer.

Every tool in the session pool gets a row saying whether this agent holds it,
through which pattern, and -- when it does not -- why not. The schemas are
built by the same registry code the runtime hands a model, so their byte count
is the one the model pays for.
"""

from __future__ import annotations

from typing import Any

from quickcode.core.permissions import DEFAULT_SPEC
from quickcode.kernel.composition import DELEGATION_TOOLS, Composition, Resolved
from quickcode.kernel.patterns import is_glob
from quickcode.kernel.resolve import expand_tool_pattern
from quickcode.server.workbench.provenance import last_prov, prov_json
from quickcode.subagents.definitions import AgentDef
from quickcode.tools.registry import ToolRegistry, build_registry


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _shell(tool: Any) -> bool:
    return bool(getattr(getattr(tool, "permission", DEFAULT_SPEC), "shell", False))


def _mutates(tool: Any) -> bool:
    return bool(getattr(getattr(tool, "permission", DEFAULT_SPEC), "mutates", True))


def grant_footer(tools: list[Any]) -> str:
    """The sentence a person actually wants about a grant.

    Not a count of checkboxes: "3 tools" says nothing, and "3 read-only, 0 that
    change files, 0 that run shell commands" says the whole thing.
    """
    total = len(tools)
    read_only = sum(1 for t in tools if t.is_read_only)
    shell = sum(1 for t in tools if _shell(t))
    writes = sum(1 for t in tools if not t.is_read_only and not _shell(t))
    return (
        f"{total} {_plural(total, 'tool', 'tools')} · "
        f"{read_only} read-only · "
        f"{writes} that {_plural(writes, 'changes', 'change')} files · "
        f"{shell} that {_plural(shell, 'runs', 'run')} shell commands"
    )


def _family(name: str) -> str:
    """The bulk family a tool belongs to, for the picker's collapsed rows.

    Only families a *pattern* can name: ``mcp__server__*`` and ``task_*``. A
    grouping the picker cannot express as one pattern would be a grouping that
    lies about what pressing "grant all" writes.
    """
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"mcp__{parts[1]}__*"
    if name.startswith("task_"):
        return "task_*"
    return ""


def _group(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP · {parts[1]}" if len(parts) >= 3 else "MCP"
    if name.startswith("task_"):
        return "Tasks"
    if name in DELEGATION_TOOLS:
        return "Subagents"
    if name in ("read", "write", "edit", "glob", "grep"):
        return "Files"
    if name == "bash":
        return "Shell"
    if name == "plan":
        return "Planning"
    return "Other"


def stated_patterns(
    preset: Any, defn: AgentDef | None, agent_id: str, is_orchestrator: bool,
) -> tuple[list[str], bool, str]:
    """The patterns the picker edits, and which layer wrote them.

    Returns ``(patterns, inherits, stated_by)``. ``inherits`` is ``tools: null``
    — "everything the spawner holds" — which is a statement and not a silence,
    and the picker must never quietly rewrite it into a frozen name list.
    """
    if is_orchestrator:
        comp = getattr(preset, "orchestrator", Composition())
        if comp.states("tools"):
            return ([] if comp.tools is None else list(comp.tools),
                    comp.tools is None, f"preset:{getattr(preset, 'id', '')}")
        return ["*"], False, "default"

    overlay = (getattr(preset, "agents", {}) or {}).get(agent_id)
    if overlay is not None and overlay.states("tools"):
        return ([] if overlay.tools is None else list(overlay.tools),
                overlay.tools is None, f"preset:{getattr(preset, 'id', '')}")
    comp = getattr(defn, "composition", Composition())
    if comp.states("tools"):
        return ([] if comp.tools is None else list(comp.tools),
                comp.tools is None, f"agent:{agent_id}")
    return ["*"], False, "default"


def _tool_row(tool: Any, *, state: str, pattern: str, provenance: dict[str, Any],
              reason: str = "") -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": (tool.description or "").strip().split("\n")[0][:200],
        "read_only": bool(tool.is_read_only),
        "shell": _shell(tool),
        "mutates": _mutates(tool),
        "group": _group(tool.name),
        "family": _family(tool.name),
        "state": state,
        "pattern": pattern,
        "provenance": provenance,
        "reason": reason,
    }


def pool_rows(
    pool: list[Any], resolved: Resolved, inherits: bool,
) -> list[dict[str, Any]]:
    """Every tool in the session pool in one of four states.

    Four, not two. ``agent``/``send_message`` are stripped by
    ``build_registry`` regardless of any allowlist and re-added by depth, and
    ``plan`` never reaches a subagent, so offering those a checkbox would offer
    a promise the runtime breaks. They are ``excluded`` and say why.
    """
    granted = set(resolved.tools)
    rows: list[dict[str, Any]] = []
    for tool in pool:
        name = tool.name
        if name in DELEGATION_TOOLS:
            rows.append(_tool_row(
                tool, state="excluded", pattern="", provenance={},
                reason=("granted by depth, never by pattern: this agent has it "
                        "because it may spawn agents"
                        if name in granted else
                        "granted by depth, never by pattern: this agent has no "
                        "spawnable agents"),
            ))
            continue
        if name == "plan" and resolved.role != "orchestrator":
            rows.append(_tool_row(
                tool, state="excluded", pattern="", provenance={},
                reason="the plan tool is interactive; a subagent has nobody to "
                       "show a plan to and never receives it",
            ))
            continue

        if name in granted:
            prov = last_prov(resolved, f"tools.{name}")
            rule = getattr(prov, "rule", "") or ("*" if inherits else "")
            by_glob = inherits or is_glob(rule) or expand_tool_pattern(rule) != name
            rows.append(_tool_row(
                tool,
                state="matched-by-glob" if by_glob else "matched",
                pattern=rule or ("inherited" if inherits else ""),
                provenance=prov_json(prov),
            ))
            continue

        reason = "no pattern in this agent's grant matches it"
        for problem in resolved.problems:
            if problem.field == "tools" and problem.code == "tool_withheld_by_parent" \
                    and f"'{name}'" in problem.message:
                reason = problem.message
        rows.append(_tool_row(tool, state="unmatched", pattern="", provenance={},
                              reason=reason))
    return rows


def schemas_for(
    resolved: Resolved, pool: list[Any],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """The exact tool objects and schemas this agent would be handed.

    Built the way the runtime builds them -- ``ToolRegistry`` over the resolved
    names at depth 0, ``build_registry`` for a child -- so the byte count in the
    header is the byte count the model pays for.
    """
    if resolved.role == "orchestrator":
        registry = ToolRegistry([t for t in pool if t.name in resolved.tools])
    else:
        registry = build_registry(
            list(resolved.tools),
            include_agent="agent" in resolved.tools,
            pool=pool,
        )
    tools = list(registry.tools.values())
    out: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.schema()
        prov = last_prov(resolved, f"tools.{tool.name}")
        body = {"name": schema.name, "description": schema.description,
                "parameters": schema.parameters}
        out.append({
            "name": tool.name,
            "read_only": bool(tool.is_read_only),
            "shell": _shell(tool),
            "schema": body,
            "granted_by": prov_json(prov),
        })
    return tools, out
