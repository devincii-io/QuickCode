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

import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.routing import Mount

from quickcode.core.permissions import Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import (
    ORCHESTRATOR_ID,
    Composition,
    Resolved,
)
from quickcode.kernel.manifest import is_shipped_agent
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.resolve import (
    resolve_composition,
    runtime_limits,
    session_pool,
)
from quickcode.server.manager import ConversationManager, SwitchRefused
from quickcode.server.workbench.prompt_view import orchestrator_prompt, subagent_prompt
from quickcode.server.workbench.provenance import last_prov, prov_json
from quickcode.server.workbench.tool_rows import (
    grant_footer,
    pool_rows,
    schemas_for,
    stated_patterns,
)
from quickcode.subagents.definitions import AgentDef, load_defs

log = logging.getLogger("quickcode.server.agents")

JSON_BODY_CAP = 1024 * 1024


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
        "builtin": is_shipped_agent(agent_id, defn),
        # The body the editor edits, so the workbench never has to guess at what
        # it is about to send as a draft.
        "prompt_body": getattr(defn, "prompt_body", ""),
    }


@dataclass(frozen=True)
class _Inputs:
    """Everything one resolution reads that a session also snapshots."""

    preset: Any
    defs: dict[str, AgentDef]
    pool: list[Any]
    max_depth: int
    # The orchestrator's composition to resolve a subagent under. None means
    # "resolve it from ``preset``", which is what a live view does; a session
    # hands its frozen one.
    orchestrator: Resolved | None = None


def _session_inputs(conv: Any) -> _Inputs | None:
    """What a spawn in this running session resolves against.

    The session snapshotted its preset, its definitions and its pool at open,
    and froze its orchestrator. Resolving a subagent "for this session" off the
    files instead would describe an agent the session cannot spawn.
    """
    deps = conv.agent.ctx.extra.get("subagent")
    if deps is None:
        return None
    return _Inputs(
        preset=deps.preset if deps.preset is not None else preset_module.resolve(
            Path(conv.manager.cwd), conv.preset_id),
        defs=deps.definitions(),
        pool=deps.session_pool(),
        max_depth=deps.limits.max_depth,
        orchestrator=conv.resolved,
    )


def _resolve_under(
    manager: ConversationManager, agent_id: str, parent_id: str, inputs: _Inputs,
) -> tuple[Resolved, Resolved | None, int]:
    """``agent_id`` resolved the way the runner resolves it: under a parent, at
    the spawner's depth. Depth 0 is where the pool carve-out lives, which is
    what makes "the orchestrator may not edit files, but its children may"
    expressible at all."""
    shared: dict[str, Any] = {
        "pool": inputs.pool, "preset": inputs.preset, "defs": inputs.defs,
        "cwd": Path(manager.cwd), "max_depth": inputs.max_depth,
        "resolve_model": manager.resolve_role,
    }
    if agent_id == ORCHESTRATOR_ID:
        return resolve_orchestrator(**shared), None, 0
    parent = inputs.orchestrator or resolve_orchestrator(**shared)
    depth = 0
    if parent_id and parent_id != ORCHESTRATOR_ID:
        parent = resolve_composition(parent_id, parent=parent, depth=0, **shared)
        depth = 1
    return resolve_composition(agent_id, parent=parent, depth=depth, **shared), parent, depth


def _resolve_view(
    manager: ConversationManager,
    agent_id: str,
    *,
    preset: Any,
    defs: dict[str, AgentDef],
    parent_id: str = "",
    conv_id: str = "",
    frozen: Resolved | None = None,
    session: _Inputs | None = None,
) -> dict[str, Any]:
    """One agent's whole answer: values, provenance, prompt bytes, schemas.

    ``frozen`` short-circuits resolution with a session's recorded snapshot, and
    ``session`` resolves a subagent against a session's snapshotted inputs; the
    live resolution still runs alongside either, so drift can be reported rather
    than guessed at.
    """
    cwd = Path(manager.cwd)
    limits = runtime_limits(cwd)
    live_inputs = _Inputs(
        preset=preset, defs=defs,
        pool=session_pool(cwd, list(manager.registry_factory().tools.values())),
        max_depth=limits.max_depth,
    )
    is_orchestrator = agent_id == ORCHESTRATOR_ID
    inputs = session or live_inputs
    defn = inputs.defs.get(agent_id)
    if not is_orchestrator and defn is None:
        raise HTTPException(404, f"no agent {agent_id!r} in this project")

    live, parent, depth = _resolve_under(manager, agent_id, parent_id, live_inputs)
    if session is not None and frozen is None:
        frozen, parent, depth = _resolve_under(manager, agent_id, parent_id, session)
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
        **_identity(agent_id, defn, inputs.preset),
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


async def _read_json(request: Request, maximum: int = JSON_BODY_CAP) -> Any:
    """A JSON body, refused once it passes ``maximum`` bytes rather than after
    the whole request has been buffered.

    Local until ``server/http.py``'s shared reader is in this tree; the two
    differ only in the wording of their 413 and 400 details.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > maximum:
        raise HTTPException(413, "request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum:
            raise HTTPException(413, "request body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
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

    orchestrator = resolve_orchestrator(
        pool=pool, preset=preset, defs=defs, cwd=cwd, max_depth=limits.max_depth,
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
    session: _Inputs | None = None
    conv = manager.get(conv_id) if conv_id else None
    if conv_id and conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    if conv is not None:
        preset_id = preset_id or conv.preset_id
        session = _session_inputs(conv)
        if agent_id == ORCHESTRATOR_ID:
            frozen = conv.resolved
    preset = preset_module.resolve(cwd, preset_id)
    defs = load_defs(cwd)
    return _resolve_view(
        manager, agent_id, preset=preset, defs=defs, parent_id=parent_id,
        conv_id=conv_id, frozen=frozen, session=session,
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
    # Read as the file states it, not as the trust gate lets a session obey it:
    # this is written back, and a gated field left out of the write would be
    # erased from the file rather than merely ignored.
    preset = preset_module.resolve(cwd, str(body.get("preset") or ""), trusted=True)
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


def _composition_id(name: str) -> str:
    """``"Review only"`` -> ``review-only``: the id a typed name is stored under,
    by the same rules an authored plugin's file name follows."""
    text = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    if text and not text[0].isalpha():
        text = f"c-{text}"
    return text[:48]


def _derive(manager: ConversationManager, preset_id: str, body: Any) -> dict[str, Any]:
    """Duplicate a composition into a project-scoped one you own.

    This is the on-ramp the switcher's last entry uses: most people discover
    they want a custom composition at the moment an existing one is nearly
    right.
    """
    cwd = Path(manager.cwd)
    # As written, for the same reason as ``_composition_write``: the copy goes
    # into the project file, where the gate applies to it on every read anyway.
    presets = preset_module.load_presets(cwd, trusted=True)
    source = presets.get(preset_id)
    if source is None:
        raise HTTPException(404, f"no composition {preset_id!r}")
    wanted = str((body or {}).get("name") or "").strip() if isinstance(body, dict) else ""
    if wanted:
        # A name somebody typed is a name they meant: it becomes the id, and a
        # clash is refused rather than quietly numbered into a different one.
        new_id = _composition_id(wanted)
        if not new_id:
            raise HTTPException(400, (
                f"{wanted!r} is not a usable composition name: it needs at least "
                "one letter or digit"))
        if new_id in presets:
            raise HTTPException(409, (
                f"a composition called {new_id!r} already exists"
                + (" and is built in" if presets[new_id].builtin else "")
                + " — pick another name, or open that one"))
    else:
        new_id = f"{preset_id}-copy"
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
