"""One agent's whole answer: values, provenance, prompt bytes, schemas.

``/resolved`` and ``/preview`` both end here. A resolution against the current
settings files says ``frozen: false``; one read out of a running session's
record says ``frozen: true`` and carries the digest it was recorded with, and
when the two disagree the payload says so rather than picking one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException

from quickcode.core.permissions import Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID, Resolved
from quickcode.kernel.manifest import is_shipped_agent
from quickcode.kernel.resolve import runtime_limits, session_pool
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.prompt_view import orchestrator_prompt, subagent_prompt
from quickcode.server.workbench.provenance import last_prov, prov_json
from quickcode.server.workbench.resolution import Inputs, resolve_under, session_inputs
from quickcode.server.workbench.tool_rows import (
    grant_footer,
    pool_rows,
    schemas_for,
    stated_patterns,
)
from quickcode.subagents.definitions import AgentDef, load_defs


def identity(agent_id: str, defn: AgentDef | None, preset: Any) -> dict[str, Any]:
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
        "builtin": is_shipped_agent(agent_id, defn),
        # The body the editor edits, so the workbench never has to guess at what
        # it is about to send as a draft.
        "prompt_body": getattr(defn, "prompt_body", ""),
    }


def resolve_view(
    manager: ConversationManager,
    agent_id: str,
    *,
    preset: Any,
    defs: dict[str, AgentDef],
    parent_id: str = "",
    conv_id: str = "",
    frozen: Resolved | None = None,
    session: Inputs | None = None,
) -> dict[str, Any]:
    """One agent's whole answer: values, provenance, prompt bytes, schemas.

    ``frozen`` short-circuits resolution with a session's recorded snapshot, and
    ``session`` resolves a subagent against a session's snapshotted inputs; the
    live resolution still runs alongside either, so drift can be reported rather
    than guessed at.
    """
    cwd = Path(manager.cwd)
    limits = runtime_limits(cwd)
    live_inputs = Inputs(
        preset=preset, defs=defs,
        pool=session_pool(cwd, list(manager.registry_factory().tools.values())),
        max_depth=limits.max_depth,
    )
    is_orchestrator = agent_id == ORCHESTRATOR_ID
    inputs = session or live_inputs
    defn = inputs.defs.get(agent_id)
    if not is_orchestrator and defn is None:
        raise HTTPException(404, f"no agent {agent_id!r} in this project")

    live, parent, depth = resolve_under(manager, agent_id, parent_id, live_inputs)
    if session is not None and frozen is None:
        frozen, parent, depth = resolve_under(manager, agent_id, parent_id, session)
    pool = inputs.pool
    resolved = frozen or live

    conv = manager.get(conv_id) if conv_id else None
    if is_orchestrator:
        model = conv.agent.model if conv is not None else (
            manager.config.last_model or manager.config.profile.resolve("orchestrator")
        )
        plan = conv is not None and conv.agent.mode == Mode.plan
        prompt = orchestrator_prompt(manager, resolved, model=model, plan=plan)
    else:
        model = manager.resolve_role(resolved.model or getattr(defn, "model", "worker"))
        draft_defn = defn
        prompt = subagent_prompt(manager, draft_defn, model=model)

    tools, schemas = schemas_for(resolved, pool)
    patterns, inherits, stated_by = stated_patterns(
        preset, defn, agent_id, is_orchestrator
    )
    rows = pool_rows(pool, resolved, inherits)

    denied = [
        {"name": row["name"], "read_only": row["read_only"], "shell": row["shell"],
         "reason": row["reason"], "state": row["state"]}
        for row in rows if row["state"] in ("unmatched", "excluded")
        and row["name"] not in resolved.tools
    ]

    spawnable = sorted(k for k in inputs.defs if k != ORCHESTRATOR_ID)
    spawns = [
        {"id": name, "granted_by": prov_json(last_prov(resolved, f"spawns.{name}"))}
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
        **identity(agent_id, defn, inputs.preset),
        "frozen": frozen is not None,
        "live": frozen is None,
        "resolved_against": {
            "preset": getattr(inputs.preset, "id", ""),
            "preset_title": getattr(inputs.preset, "title", ""),
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
        "footer": grant_footer(tools),
        "models": {
            "model": resolved.model,
            "resolved": manager.resolve_role(resolved.model) if resolved.model else "",
            "allowed": list(resolved.models),
            "selectable": resolved.model_selectable,
            "provenance": prov_json(last_prov(resolved, "model")),
        },
        "limits": {
            "ceiling": resolved.ceiling.value,
            "ceiling_provenance": prov_json(last_prov(resolved, "ceiling")),
            "max_turns": resolved.max_turns,
            "max_turns_applies": resolved.role != "orchestrator",
            "max_depth": limits.max_depth,
            "max_agents": limits.max_agents,
            "max_rounds": limits.max_rounds,
        },
        "problems": [p.to_json() for p in resolved.problems],
    }
    return payload


def resolved_payload(
    manager: ConversationManager, agent_id: str, *,
    preset_id: str = "", parent_id: str = "", conv_id: str = "",
) -> dict[str, Any]:
    cwd = Path(manager.cwd)
    frozen: Resolved | None = None
    session: Inputs | None = None
    conv = manager.get(conv_id) if conv_id else None
    if conv_id and conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    if conv is not None:
        preset_id = preset_id or conv.preset_id
        session = session_inputs(conv)
        if agent_id == ORCHESTRATOR_ID:
            frozen = conv.resolved
    preset = preset_module.resolve(cwd, preset_id)
    defs = load_defs(cwd)
    return resolve_view(
        manager, agent_id, preset=preset, defs=defs, parent_id=parent_id,
        conv_id=conv_id, frozen=frozen, session=session,
    )
