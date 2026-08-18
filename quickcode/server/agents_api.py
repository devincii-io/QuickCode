"""The agent workbench's backend: what an agent gets, and why it does not get
the rest.

Three ideas hold this module together.

**One computation, never two.** ``/resolved`` and ``/preview`` call
``resolve_composition`` -- the same function ``manager.open()`` and
``spawn_subagent`` call -- and render the prompt through
``prompts/system.render_with_sections`` or ``prompts/subagent`` -- the same code
the runner renders with. Nothing here reconstructs a prompt or a tool list. A
reconstruction drifts, and a preview that drifts is worse than no preview,
because it is believed.

**Absences are answers.** A tool that is missing is listed with the reason it is
missing; a prompt section that did not render is listed with the reason it did
not. "You cannot see why you don't have it" is the failure this whole surface
exists to fix, so an omitted key is never the answer to "why".

**Live is labelled live.** A resolution against the current settings files says
``frozen: false``; a resolution read out of a running session's meta record says
``frozen: true`` and carries the digest it was recorded with. When the two
disagree the payload says so rather than picking one.

Routes are registered from here rather than from ``server/app.py`` following the
``server/gitinfo.py:register_git_routes`` precedent, so the app's diff is one
import and one call.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.routing import Mount

from quickcode.core.permissions import DEFAULT_SPEC, Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    DELEGATION_TOOLS,
    ORCHESTRATOR_ID,
    Composition,
    Resolved,
)
from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
from quickcode.prompts import sections as sections_module
from quickcode.prompts.subagent import render_subagent_prompt
from quickcode.prompts.system import render_with_sections
from quickcode.server.manager import ConversationManager, SwitchRefused
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.tools.registry import ALIASES, ToolRegistry, build_registry

log = logging.getLogger("quickcode.server.agents")

JSON_BODY_CAP = 1024 * 1024
_GLOB_CHARS = ("*", "?", "[")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in _GLOB_CHARS)


def _expand(pattern: str) -> str:
    """A pattern as ``select()`` sees it: aliases resolved, whitespace gone."""
    text = (pattern or "").strip()
    return ALIASES.get(text, text)


def _prov_json(prov: Any) -> dict[str, Any]:
    return prov.to_json() if prov is not None else {}


def _last_prov(resolved: Resolved, key: str) -> Any:
    entries = resolved.chain.get(key) or ()
    return entries[-1] if entries else None


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _shell(tool: Any) -> bool:
    return bool(getattr(getattr(tool, "permission", DEFAULT_SPEC), "shell", False))


def _mutates(tool: Any) -> bool:
    return bool(getattr(getattr(tool, "permission", DEFAULT_SPEC), "mutates", True))


def _footer(tools: list[Any]) -> str:
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


# --------------------------------------------------------------------------
# prompt rendering, through the real code paths
# --------------------------------------------------------------------------

# Why an internal section can be absent from a composed prompt. ``compose()``
# drops an empty section, and an empty region is invisible: without this table a
# user who set ``skip_project_instructions`` has no way to discover that the
# flag is what removed the block.
_ABSENCE_REASONS: dict[str, str] = {
    "prompt.project_instructions":
        "no QUICKCODE.md / AGENTS.md / CLAUDE.md was found in this project, so "
        "there is nothing to include",
    "prompt.orchestration":
        "this agent has no spawnable agents, so the delegation playbook is not "
        "sent",
    "prompt.send_message_hint":
        "this agent has no spawnable agents, so there is nothing to resume",
    "prompt.plan_mode":
        "this session is not in plan mode",
    "prompt.headless":
        "this is an interactive session, not a headless run",
}

_SUBAGENT_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("identity", "Identity", "generated from the agent's name and model"),
    ("role", "Instructions", "the agent's own body — this is what you edit"),
    ("environment", "Environment", "generated from the session's environment"),
    ("project_instructions", "Project instructions",
     "the project's QUICKCODE.md / AGENTS.md / CLAUDE.md"),
)


def _tag_span(text: str, tag: str) -> tuple[int, int] | None:
    open_at = text.find(f"<{tag}")
    if open_at < 0:
        return None
    close = text.find(f"</{tag}>", open_at)
    if close < 0:
        return None
    return open_at, close + len(f"</{tag}>")


def _orchestrator_prompt(
    manager: ConversationManager, resolved: Resolved, *, model: str, plan: bool,
) -> dict[str, Any]:
    """The orchestrator's composed prompt, rendered by ``compose()`` itself."""
    text, rendered = render_with_sections(
        manager.env,
        model=model,
        provider=manager.provider_name,
        headless=False,
        plan=plan,
        orchestration=bool(resolved.spawns),
        overrides=dict(resolved.section_bodies),
    )
    present = {section.id for section in rendered}
    blocks = [
        {
            "id": section.id,
            "title": section.title,
            "tier": section.tier,
            "start": section.start,
            "end": section.end,
            "overridden": section.id in resolved.section_bodies,
            "provenance": _prov_json(
                _last_prov(resolved, f"section_bodies.{section.id}")
            ),
        }
        for section in rendered
    ]
    absences = [
        {
            "id": section.id,
            "title": section.title,
            "reason": _ABSENCE_REASONS.get(
                section.id,
                "its body resolved empty, and empty sections are dropped from "
                "the composition",
            ),
        }
        for section in sections_module.ordered()
        if section.id not in present
    ]
    return {"text": text, "blocks": blocks, "absences": absences,
            "template": "prompts/sections.py"}


def _subagent_prompt(
    manager: ConversationManager, defn: AgentDef, *, model: str,
) -> dict[str, Any]:
    """A subagent's prompt, rendered by ``render_subagent_prompt`` itself.

    A subagent's prompt is a different template, not a narrowed version of the
    orchestrator's, and that fact is the single most surprising thing about
    prompt sections: editing ``prompt.tone`` does nothing here. It is stated as
    an absence rather than left to be discovered.
    """
    text = render_subagent_prompt(defn, manager.env, model=model)
    blocks: list[dict[str, Any]] = []
    for tag, title, note in _SUBAGENT_BLOCKS:
        span = _tag_span(text, tag)
        if span is None:
            continue
        blocks.append({
            "id": f"subagent.{tag}", "title": title, "tier": "free",
            "start": span[0], "end": span[1], "overridden": tag == "role",
            "note": note,
            "provenance": {
                "layer": "agent" if tag == "role" else "default",
                "source": defn.path or "prompts/subagent.py",
                "rule": "body" if tag == "role" else tag,
            },
        })

    absences: list[dict[str, Any]] = [{
        "id": "prompt.*",
        "title": "Every prompt section",
        "reason": (
            "a subagent's prompt is composed from prompts/subagent.py, not from "
            "the section list — none of the orchestrator's prompt sections "
            "reaches this agent"
        ),
    }]
    if defn.skip_project_instructions:
        absences.append({
            "id": "prompt.project_instructions",
            "title": "Project instructions",
            "reason": "omitted — this agent sets skip_project_instructions",
        })
    elif not manager.env.project_instructions.strip():
        absences.append({
            "id": "prompt.project_instructions",
            "title": "Project instructions",
            "reason": "no QUICKCODE.md / AGENTS.md / CLAUDE.md was found in this "
                      "project",
        })
    return {"text": text, "blocks": blocks, "absences": absences,
            "template": "prompts/subagent.py"}


# --------------------------------------------------------------------------
# the tool view
# --------------------------------------------------------------------------

def _stated_patterns(
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


def _pool_rows(
    pool: list[Any], resolved: Resolved, patterns: list[str], inherits: bool,
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
            prov = _last_prov(resolved, f"tools.{name}")
            rule = getattr(prov, "rule", "") or ("*" if inherits else "")
            by_glob = inherits or _is_glob(rule) or _expand(rule) != name
            rows.append(_tool_row(
                tool,
                state="matched-by-glob" if by_glob else "matched",
                pattern=rule or ("inherited" if inherits else ""),
                provenance=_prov_json(prov),
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


def _schemas_for(
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
        prov = _last_prov(resolved, f"tools.{tool.name}")
        body = {"name": schema.name, "description": schema.description,
                "parameters": schema.parameters}
        out.append({
            "name": tool.name,
            "read_only": bool(tool.is_read_only),
            "shell": _shell(tool),
            "schema": body,
            "granted_by": _prov_json(prov),
        })
    return tools, out


# --------------------------------------------------------------------------
# the resolution itself
# --------------------------------------------------------------------------

def _identity(agent_id: str, defn: AgentDef | None, preset: Any) -> dict[str, Any]:
    if agent_id == ORCHESTRATOR_ID:
        return {
            "id": ORCHESTRATOR_ID,
            "title": "Orchestrator",
            "description": (
                "The agent a session starts as. Its tools, prompt and starting "
                "mode come from the active composition."
            ),
            "role": "orchestrator",
            "source": f"preset:{getattr(preset, 'id', '')}",
            "path": "",
            "builtin": True,
            "prompt_body": "",
        }
    return {
        "id": agent_id,
        "title": getattr(defn, "name", agent_id),
        "description": getattr(defn, "description", ""),
        "role": getattr(defn, "role", "subagent"),
        "source": getattr(defn, "source", "internal"),
        "path": getattr(defn, "path", ""),
        "builtin": agent_id in ("explore", "general"),
        # The body the editor edits, so the workbench never has to guess at what
        # it is about to send as a draft.
        "prompt_body": getattr(defn, "prompt_body", ""),
    }


def _resolve_view(
    manager: ConversationManager,
    agent_id: str,
    *,
    preset: Any,
    defs: dict[str, AgentDef],
    parent_id: str = "",
    conv_id: str = "",
    frozen: Resolved | None = None,
) -> dict[str, Any]:
    """One agent's whole answer: values, provenance, prompt bytes, schemas.

    ``frozen`` short-circuits resolution with a session's recorded snapshot; the
    live resolution still runs alongside it so drift can be reported rather than
    guessed at.
    """
    cwd = Path(manager.cwd)
    pool = session_pool(cwd, list(manager.registry_factory().tools.values()))
    limits = runtime_limits(cwd)
    is_orchestrator = agent_id == ORCHESTRATOR_ID
    defn = defs.get(agent_id)
    if not is_orchestrator and defn is None:
        raise HTTPException(404, f"no agent {agent_id!r} in this project")

    # A subagent is resolved the way the runner resolves it: under a parent, at
    # the spawner's depth. Depth 0 is where the pool carve-out lives, which is
    # what makes "the orchestrator may not edit files, but its children may"
    # expressible at all.
    parent: Resolved | None = None
    depth = 0
    if not is_orchestrator:
        parent_id = parent_id or ORCHESTRATOR_ID
        parent = resolve_composition(
            ORCHESTRATOR_ID, pool=pool, preset=preset, defs=defs, cwd=cwd,
            parent=None, depth=0, max_depth=limits.max_depth,
            resolve_model=manager.resolve_role,
        )
        if parent_id != ORCHESTRATOR_ID:
            parent = resolve_composition(
                parent_id, pool=pool, preset=preset, defs=defs, cwd=cwd,
                parent=parent, depth=0, max_depth=limits.max_depth,
                resolve_model=manager.resolve_role,
            )
            depth = 1

    live = resolve_composition(
        agent_id, pool=pool, preset=preset, defs=defs, cwd=cwd,
        parent=parent, depth=depth, max_depth=limits.max_depth,
        resolve_model=manager.resolve_role,
    )
    resolved = frozen or live

    conv = manager.get(conv_id) if conv_id else None
    if is_orchestrator:
        model = conv.agent.model if conv is not None else (
            manager.config.last_model or manager.config.profile.resolve("orchestrator")
        )
        plan = conv is not None and conv.agent.mode == Mode.plan
        prompt = _orchestrator_prompt(manager, resolved, model=model, plan=plan)
    else:
        model = manager.resolve_role(resolved.model or getattr(defn, "model", "worker"))
        draft_defn = defn
        prompt = _subagent_prompt(manager, draft_defn, model=model)

    tools, schemas = _schemas_for(resolved, pool)
    patterns, inherits, stated_by = _stated_patterns(
        preset, defn, agent_id, is_orchestrator
    )
    rows = _pool_rows(pool, resolved, patterns, inherits)

    denied = [
        {"name": row["name"], "read_only": row["read_only"], "shell": row["shell"],
         "reason": row["reason"], "state": row["state"]}
        for row in rows if row["state"] in ("unmatched", "excluded")
        and row["name"] not in resolved.tools
    ]

    spawnable = sorted(k for k in defs if k != ORCHESTRATOR_ID)
    spawns = [
        {"id": name, "granted_by": _prov_json(_last_prov(resolved, f"spawns.{name}"))}
        for name in resolved.spawns
    ]
    denied_spawns = [
        {"id": name,
         "reason": ("this agent's spawn patterns do not name it"
                    if depth == 0 and parent is None else
                    "not spawnable here — either this agent's patterns do not "
                    "name it, or the spawning agent may not spawn it either")}
        for name in spawnable if name not in resolved.spawns
    ]

    schema_bytes = sum(
        len(str(entry["schema"]).encode("utf-8")) for entry in schemas
    )
    payload: dict[str, Any] = {
        **_identity(agent_id, defn, preset),
        "frozen": frozen is not None,
        "live": frozen is None,
        "resolved_against": {
            "preset": getattr(preset, "id", ""),
            "preset_title": getattr(preset, "title", ""),
            "parent": parent.id if parent is not None else "",
            "depth": depth,
            "conv": conv_id,
        },
        "digest": resolved.digest(),
        "drift": (
            {"frozen_digest": resolved.digest(), "live_digest": live.digest(),
             "changed": resolved.digest() != live.digest()}
            if frozen is not None else None
        ),
        "resolved": resolved.to_json(),
        "prompt": {
            **prompt,
            "chars": len(prompt["text"]),
            "bytes": len(prompt["text"].encode("utf-8")),
        },
        "tools": schemas,
        "schema_bytes": schema_bytes,
        "denied": denied,
        "pool": rows,
        "grant": {
            "patterns": patterns,
            "inherits": inherits,
            "stated_by": stated_by,
            "editable": not getattr(preset, "builtin", False),
            "editable_reason": (
                "" if not getattr(preset, "builtin", False) else
                f"“{getattr(preset, 'title', '')}” is a built-in composition. "
                "Duplicate it into this project to edit it."
            ),
        },
        "spawns": spawns,
        "denied_spawns": denied_spawns,
        "footer": _footer(tools),
        "models": {
            "model": resolved.model,
            "resolved": manager.resolve_role(resolved.model) if resolved.model else "",
            "allowed": list(resolved.models),
            "selectable": resolved.model_selectable,
            "provenance": _prov_json(_last_prov(resolved, "model")),
        },
        "limits": {
            "ceiling": resolved.ceiling.value,
            "ceiling_provenance": _prov_json(_last_prov(resolved, "ceiling")),
            "max_turns": resolved.max_turns,
            "max_turns_applies": resolved.role != "orchestrator",
            "max_depth": limits.max_depth,
            "max_agents": limits.max_agents,
            "max_rounds": limits.max_rounds,
        },
        "problems": [p.to_json() for p in resolved.problems],
    }
    return payload


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------

def _draft_defs(
    defs: dict[str, AgentDef], agent_id: str, comp: dict[str, Any] | None,
    body: str | None,
) -> dict[str, AgentDef]:
    """A definitions snapshot with one agent replaced by an unsaved draft.

    The draft is a real ``AgentDef`` so it travels through exactly the same
    resolver and the same prompt renderer as a saved one. Nothing is written.
    """
    base = defs.get(agent_id)
    if base is None:
        return defs
    composition = base.composition
    if comp:
        composition = Composition.from_dict({**base.composition.to_dict(), **comp})
    out = dict(defs)
    out[agent_id] = AgentDef(
        name=base.name,
        description=base.description,
        role=base.role,
        composition=composition,
        source=base.source,
        path=base.path,
        prompt_body=base.prompt_body if body is None else body,
    )
    return out


def _draft_preset(preset: Any, comp: dict[str, Any] | None) -> Any:
    if not comp:
        return preset
    merged = Composition.from_dict({**preset.orchestrator.to_dict(), **comp})
    return replace(preset, orchestrator=merged)


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    if len(raw) > JSON_BODY_CAP:
        raise HTTPException(413, "request body too large")
    if not raw:
        return {}
    import json

    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, f"malformed JSON: {exc}") from exc


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------

def _agents_payload(manager: ConversationManager) -> dict[str, Any]:
    """Every agent identity, ``@orchestrator`` first and first-class.

    The orchestrator is not a definition on disk, which is exactly why it has to
    appear here: an inventory that lists the spawnable agents and omits the one
    you are talking to answers the wrong question.
    """
    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd)
    defs = load_defs(cwd)
    pool = session_pool(cwd, list(manager.registry_factory().tools.values()))
    limits = runtime_limits(cwd)

    orchestrator = resolve_composition(
        ORCHESTRATOR_ID, pool=pool, preset=preset, defs=defs, cwd=cwd,
        parent=None, depth=0, max_depth=limits.max_depth,
        resolve_model=manager.resolve_role,
    )
    rows = [{
        **_identity(ORCHESTRATOR_ID, None, preset),
        "tool_count": len(orchestrator.tools),
        "denied_count": len(orchestrator.denied_tools),
        "model": orchestrator.model or (
            manager.config.last_model or manager.config.profile.resolve("orchestrator")
        ),
        "ceiling": orchestrator.ceiling.value,
        "spawns": list(orchestrator.spawns),
        "problems": len(orchestrator.problems),
    }]
    for agent_id in sorted(k for k in defs if k != ORCHESTRATOR_ID):
        child = resolve_composition(
            agent_id, pool=pool, preset=preset, defs=defs, cwd=cwd,
            parent=orchestrator, depth=0, max_depth=limits.max_depth,
            resolve_model=manager.resolve_role,
        )
        rows.append({
            **_identity(agent_id, defs[agent_id], preset),
            "tool_count": len(child.tools),
            "denied_count": len(child.denied_tools),
            "model": child.model,
            "ceiling": child.ceiling.value,
            "spawns": list(child.spawns),
            "problems": len(child.problems),
        })
    return {"agents": rows, "preset": preset.id, "preset_title": preset.title}


def _resolved_payload(
    manager: ConversationManager, agent_id: str, *,
    preset_id: str = "", parent_id: str = "", conv_id: str = "",
) -> dict[str, Any]:
    cwd = Path(manager.cwd)
    frozen: Resolved | None = None
    conv = manager.get(conv_id) if conv_id else None
    if conv_id and conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    if conv is not None:
        preset_id = preset_id or conv.preset_id
        if agent_id == ORCHESTRATOR_ID:
            frozen = conv.resolved
    preset = preset_module.resolve(cwd, preset_id)
    defs = load_defs(cwd)
    return _resolve_view(
        manager, agent_id, preset=preset, defs=defs, parent_id=parent_id,
        conv_id=conv_id, frozen=frozen,
    )


def _preview_payload(
    manager: ConversationManager, agent_id: str, body: Any,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    comp = body.get("composition")
    if comp is not None and not isinstance(comp, dict):
        raise HTTPException(400, "composition must be an object")
    prompt_body = body.get("prompt_body")
    if prompt_body is not None and not isinstance(prompt_body, str):
        raise HTTPException(400, "prompt_body must be a string")

    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd, str(body.get("preset") or ""))
    defs = load_defs(cwd)
    if agent_id == ORCHESTRATOR_ID:
        preset = _draft_preset(preset, comp)
    else:
        if agent_id not in defs:
            raise HTTPException(404, f"no agent {agent_id!r} in this project")
        defs = _draft_defs(defs, agent_id, comp, prompt_body)

    payload = _resolve_view(
        manager, agent_id, preset=preset, defs=defs,
        parent_id=str(body.get("parent") or ""),
    )
    # A preview resolves against the current files with an unsaved draft on top,
    # so it is live by construction and says so. It is never frozen and it never
    # writes.
    payload["frozen"] = False
    payload["live"] = True
    payload["draft"] = True
    return payload


def _composition_write(
    manager: ConversationManager, agent_id: str, body: Any,
) -> dict[str, Any]:
    """Save a composition edit into a project-scoped preset.

    Refused on a built-in composition with the reason and the recourse, rather
    than silently forking one: which composition a session runs is a name the
    user chose, and a write that quietly changes what that name means is worse
    than a refusal.
    """
    if not isinstance(body, dict) or not isinstance(body.get("composition"), dict):
        raise HTTPException(400, "body must be {'composition': {...}}")
    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd, str(body.get("preset") or ""))
    if preset.builtin:
        raise HTTPException(
            409,
            f"“{preset.title}” is a built-in composition and cannot be edited. "
            f"Duplicate it into this project first.",
        )
    incoming = body["composition"]
    if agent_id == ORCHESTRATOR_ID:
        merged = Composition.from_dict({**preset.orchestrator.to_dict(), **incoming})
        updated = replace(preset, orchestrator=merged)
    else:
        agents = dict(preset.agents)
        current = agents.get(agent_id, Composition())
        agents[agent_id] = Composition.from_dict({**current.to_dict(), **incoming})
        updated = replace(preset, agents=agents)
    preset_module.save_preset(cwd, updated)
    return {
        "preset": updated.id,
        "applies_to": "new sessions, and any running session you switch",
        "composition": (updated.orchestrator if agent_id == ORCHESTRATOR_ID
                        else updated.agents[agent_id]).to_dict(),
    }


def _derive(manager: ConversationManager, preset_id: str, body: Any) -> dict[str, Any]:
    """Duplicate a composition into a project-scoped one you own.

    This is the on-ramp the switcher's last entry uses: most people discover
    they want a custom composition at the moment an existing one is nearly
    right.
    """
    cwd = Path(manager.cwd)
    presets = preset_module.load_presets(cwd)
    source = presets.get(preset_id)
    if source is None:
        raise HTTPException(404, f"no composition {preset_id!r}")
    wanted = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
    new_id = wanted or f"{preset_id}-copy"
    if new_id in presets:
        n = 2
        while f"{new_id}-{n}" in presets:
            n += 1
        new_id = f"{new_id}-{n}"
    copy = replace(
        source,
        id=new_id,
        title=f"{source.title} (yours)" if not wanted else wanted,
        description=source.description or f"Derived from {source.title}.",
        builtin=False,
    )
    preset_module.save_preset(cwd, copy)
    return {"id": new_id, "title": copy.title, "derived_from": preset_id,
            "path": str(preset_module.project_settings_path(cwd))}


def _switch(manager: ConversationManager, conv_id: str, body: Any) -> dict[str, Any]:
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    preset_id = (body or {}).get("preset") if isinstance(body, dict) else None
    if not isinstance(preset_id, str) or not preset_id.strip():
        raise HTTPException(400, "body must be {'preset': <id>}")
    if preset_id not in preset_module.load_presets(Path(manager.cwd)):
        raise HTTPException(404, f"no composition {preset_id!r}")
    try:
        return conv.switch_composition(preset_id.strip())
    except SwitchRefused as exc:
        # 409, not 400: the request is valid and would be valid again in a
        # moment. Refusing with the reason is the whole contract -- a switch
        # that lands invisibly three seconds later is worse than one that does
        # not happen.
        raise HTTPException(409, str(exc)) from exc


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register_agent_routes(app: FastAPI, hub: Any) -> None:
    """Mount the workbench routes, unscoped and project-scoped alike.

    ``hub`` is a ``ProjectHub``: ``hub.default`` is the launch directory and
    ``hub.get(pid)`` is whichever project the UI is showing. Both shapes run one
    pair of handlers, matching the rest of the API.
    """

    def _project(pid: str) -> ConversationManager:
        manager = hub.get(pid)
        if manager is None:
            raise HTTPException(404, f"unknown project: {pid}")
        return manager

    # ---- inventory ----

    @app.get("/api/kernel/agents")
    def agents() -> dict:
        return _agents_payload(hub.default)

    @app.get("/api/projects/{pid}/kernel/agents")
    def project_agents(pid: str) -> dict:
        return _agents_payload(_project(pid))

    # ---- resolved ----

    @app.get("/api/kernel/agents/{agent_id}/resolved")
    def resolved(agent_id: str, preset: str = "", parent: str = "",
                 conv: str = "") -> dict:
        return _resolved_payload(hub.default, agent_id, preset_id=preset,
                                 parent_id=parent, conv_id=conv)

    @app.get("/api/projects/{pid}/kernel/agents/{agent_id}/resolved")
    def project_resolved(pid: str, agent_id: str, preset: str = "",
                         parent: str = "", conv: str = "") -> dict:
        return _resolved_payload(_project(pid), agent_id, preset_id=preset,
                                 parent_id=parent, conv_id=conv)

    # ---- preview ----

    @app.post("/api/kernel/agents/{agent_id}/preview")
    async def preview(agent_id: str, request: Request) -> dict:
        return _preview_payload(hub.default, agent_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/agents/{agent_id}/preview")
    async def project_preview(pid: str, agent_id: str, request: Request) -> dict:
        return _preview_payload(_project(pid), agent_id, await _read_json(request))

    # ---- saving a composition edit ----

    @app.put("/api/kernel/agents/{agent_id}/composition")
    async def write_composition(agent_id: str, request: Request) -> dict:
        return _composition_write(hub.default, agent_id, await _read_json(request))

    @app.put("/api/projects/{pid}/kernel/agents/{agent_id}/composition")
    async def project_write_composition(pid: str, agent_id: str,
                                        request: Request) -> dict:
        return _composition_write(_project(pid), agent_id, await _read_json(request))

    # ---- duplicate-to-customise ----

    @app.post("/api/kernel/compositions/{preset_id}/derive")
    async def derive(preset_id: str, request: Request) -> dict:
        return _derive(hub.default, preset_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/compositions/{preset_id}/derive")
    async def project_derive(pid: str, preset_id: str, request: Request) -> dict:
        return _derive(_project(pid), preset_id, await _read_json(request))

    # ---- session-scoped switching ----

    @app.post("/api/kernel/conversations/{conv_id}/composition")
    async def switch(conv_id: str, request: Request) -> dict:
        return _switch(hub.default, conv_id, await _read_json(request))

    @app.post("/api/projects/{pid}/kernel/conversations/{conv_id}/composition")
    async def project_switch(pid: str, conv_id: str, request: Request) -> dict:
        return _switch(_project(pid), conv_id, await _read_json(request))

    # The frontend is mounted at "/" and matches every path, so it has to stay
    # last whatever order this module is registered in. ``app.py`` calls us
    # before the mount; anything registering afterwards (a test building the app
    # first, an embedder) would otherwise get 405s from the static handler.
    # ``sort`` is stable, so this only moves the catch-all mounts.
    app.router.routes.sort(key=lambda r: isinstance(r, Mount) and r.path in ("", "/"))
