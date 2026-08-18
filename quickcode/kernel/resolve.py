"""The resolver: one computable answer to "what does this agent actually get".

This is the only module that knows the layer order, and the layer table lives
here as code:

===  ==========  =======================================  ==========  =========
 #   Layer       Source                                   Capability  Value
===  ==========  =======================================  ==========  =========
 0   default     dataclass defaults, the ``enabled`` pool  seed        seed
 1   user        ``~/.quickcode/settings.json``            n           overwrite
 2   project     ``<cwd>/.quickcode/settings.json``        n           overwrite
 3   preset      ``presets.<id>`` and its bindings         n           overwrite
 4   agent       the ``AgentDef``'s composition            n           overwrite
 5   session     recorded in session meta at open          n           overwrite
 6   call        the ``agent`` tool's ``model=``           may not      overwrite
                                                          widen
 --  parent      ``parent.tools`` (depth >= 1) or the      n, last     --
                 session pool (depth 0)
 --  runtime     ``max_depth``, delegation-by-depth        n           --
===  ==========  =======================================  ==========  =========

``settings.local.json`` is not a layer here. It is where accreted "always
allow" rules go, not configuration; a ``plugins`` or ``presets`` key found
there produces an ``info`` problem rather than silently doing nothing.

Two properties this module must keep.

**Resolution is total.** ``resolve_composition`` never raises. It returns a
``Resolved`` carrying the answer plus problems classed error/warning/info. A
session must always open: refusing to show a conversation because a preset went
stale is worse than losing the composition.

**Spawning is fallible.** The caller refuses on any error-severity problem,
before an agent id is minted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from quickcode.core.permissions import Mode
from quickcode.kernel import state as state_store
from quickcode.kernel.composition import (
    DELEGATION_TOOLS,
    ORCHESTRATOR_ID,
    Binding,
    Composition,
    Resolved,
    Role,
    narrower_mode,
    selector_matches,
)
from quickcode.kernel.problems import Layer, Problem, Provenance

# Kept in step with ``subagents.runner.MAX_DEPTH``; passed in rather than
# imported so the kernel never depends on the runtime that calls it.
DEFAULT_MAX_DEPTH = 2

_GLOB_CHARS = ("*", "?", "[")


def _is_pattern(text: str) -> bool:
    return any(ch in text for ch in _GLOB_CHARS)


def _matches(pattern: str, name: str) -> bool:
    return pattern == name or fnmatchcase(name, pattern)


def _admits(patterns: Iterable[str], candidate: str) -> bool:
    return any(_matches(p, candidate) for p in patterns)


# --------------------------------------------------------------------------
# layer assembly
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Layer:
    """One configuration layer's contribution, with where it came from."""

    name: Layer
    source: str
    path: str
    comp: Composition

    def prov(self, rule: str = "", note: str = "") -> Provenance:
        return Provenance(layer=self.name, source=self.source, path=self.path,
                          rule=rule, note=note)


def _prompt_bodies(entries: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Section bodies a settings layer sets, straight off plugin state."""
    out: dict[str, str] = {}
    for plugin_id, entry in entries.items():
        if not plugin_id.startswith("prompt."):
            continue
        body = (entry.get("settings") or {}).get("body")
        if isinstance(body, str) and body.strip():
            out[plugin_id] = body
    return out


def _plugin_settings(entries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for plugin_id, entry in entries.items():
        settings = entry.get("settings")
        if isinstance(settings, dict) and settings:
            out[plugin_id] = dict(settings)
    return out


def _expand_bases(comp: Composition, defs: dict[str, Any]) -> list[Composition]:
    """``base: "<id>"`` depth-first, base first, cycles resolving empty.

    A base is layer-4 input, so it never escapes the parent intersection: an
    agent saying "like general, plus bash" still cannot end up with more than
    its parent holds.
    """
    chain: list[Composition] = []
    seen: set[str] = set()
    current = comp
    while current.base and current.base not in seen:
        seen.add(current.base)
        parent_def = defs.get(current.base)
        parent_comp = getattr(parent_def, "composition", None)
        if parent_comp is None:
            break
        chain.append(parent_comp)
        current = parent_comp
    chain.reverse()
    chain.append(comp)
    return chain


def _binding_contributions(
    bindings: Iterable[Binding], agent_id: str, role: Role, comp: Composition,
) -> tuple[Composition, list[str], list[Binding]]:
    """Desugar bindings that reach this agent into composition edits.

    A binding is a statement about a *relationship* and neither end owns it,
    which is why it lives in the preset and not in a plugin's frontmatter --
    a cloned repository must not ship a tool that attaches itself to your
    orchestrator. Here it stops being a separate concept: grants extend the
    preset layer's pattern lists, sets write bodies and settings, and revokes
    are collected for subtraction after the intersection.

    A grant against a field the layer does not state is a no-op, and correctly
    so: "inherit everything" already includes it, and turning inheritance into
    a one-item allowlist is the opposite of what a grant means.
    """
    revoked: list[str] = []
    unreached: list[Binding] = []
    tools = list(comp.tools) if comp.tools is not None else None
    spawns = list(comp.spawns) if comp.spawns is not None else None
    sections = list(comp.sections) if comp.sections is not None else None
    bodies = dict(comp.section_bodies)
    settings = {k: dict(v) for k, v in comp.settings.items()}
    touched = False

    for binding in bindings:
        if not selector_matches(binding.to, agent_id, role):
            unreached.append(binding)
            continue
        plugin = binding.plugin
        pattern = ""
        target = ""
        if plugin.startswith("tool."):
            pattern, target = plugin[len("tool."):], "tools"
        elif plugin.startswith("mcp."):
            pattern, target = f"mcp__{plugin[len('mcp.'):]}__*", "tools"
        elif plugin.startswith("agent."):
            pattern, target = plugin[len("agent."):], "spawns"
        elif plugin.startswith("prompt."):
            pattern, target = plugin, "sections"

        if binding.effect == "revoke":
            if target == "tools" and pattern:
                revoked.append(pattern)
                touched = True
            continue

        if binding.effect == "set":
            if plugin.startswith("prompt.") and isinstance(binding.value, str):
                bodies[plugin] = binding.value
                touched = True
            elif isinstance(binding.value, dict):
                settings.setdefault(plugin, {}).update(binding.value)
                touched = True
            continue

        # grant
        if target == "tools" and tools is not None and pattern not in tools:
            tools.append(pattern)
            touched = True
        elif target == "spawns" and spawns is not None and pattern not in spawns:
            spawns.append(pattern)
            touched = True
        elif target == "sections" and pattern:
            sections = sections if sections is not None else []
            if pattern not in sections:
                sections.append(pattern)
                touched = True

    if not touched:
        return comp, revoked, unreached

    updates: dict[str, Any] = {}
    if comp.states("tools") and tools is not None:
        updates["tools"] = tuple(tools)
    if comp.states("spawns") and spawns is not None:
        updates["spawns"] = tuple(spawns)
    if sections is not None:
        updates["sections"] = tuple(sections)
    if bodies != comp.section_bodies:
        updates["section_bodies"] = bodies
    if settings != comp.settings:
        updates["settings"] = settings
    return comp.with_fields(**updates), revoked, unreached


# --------------------------------------------------------------------------
# capability intersection
# --------------------------------------------------------------------------

def _intersect_named(
    layers: list[_Layer],
    field: str,
    candidates: list[str],
) -> tuple[set[str], dict[str, list[Provenance]], list[tuple[_Layer, str]], set[str]]:
    """Intersect one pattern-valued capability field across every layer.

    Returns the surviving names, a provenance chain per name, the (layer,
    pattern) pairs that matched nothing, and the set of literal names any layer
    asked for by name. Layers that state nothing contribute the identity, which
    is what makes the result independent of the order they are visited in.
    """
    survivors = set(candidates)
    chains: dict[str, list[Provenance]] = {name: [] for name in candidates}
    empty_patterns: list[tuple[_Layer, str]] = []
    literals: set[str] = set()

    for layer in layers:
        patterns = getattr(layer.comp, field)
        if not layer.comp.states(field) or patterns is None:
            continue
        matched: set[str] = set()
        for pattern in patterns:
            hits = [name for name in candidates if _matches(pattern, name)]
            if not _is_pattern(pattern):
                literals.add(pattern)
            if not hits:
                empty_patterns.append((layer, pattern))
                continue
            for name in hits:
                matched.add(name)
                chains[name].append(layer.prov(rule=pattern))
        survivors &= matched

    for name in candidates:
        if not chains[name]:
            chains[name] = [Provenance(layer="default", source="pool", rule="*")]
    return survivors, chains, empty_patterns, literals


def _intersect_models(layers: list[_Layer]) -> tuple[tuple[str, ...], list[Provenance]]:
    """Intersect allowed-model sets. Empty means "any", the identity."""
    current: tuple[str, ...] | None = None
    chain: list[Provenance] = []
    for layer in layers:
        if not layer.comp.states("models") or not layer.comp.models:
            continue
        incoming = tuple(layer.comp.models)
        chain.append(layer.prov(rule=", ".join(incoming)))
        if current is None:
            current = incoming
            continue
        merged = [p for p in current if _admits(incoming, p)]
        merged += [p for p in incoming if _admits(current, p) and p not in merged]
        current = tuple(merged)
    return (current or ()), chain


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def resolve_composition(
    agent_id: str,
    *,
    pool: list[Any],
    preset: Any,
    defs: dict[str, Any],
    cwd: Path | None = None,
    parent: Resolved | None = None,
    depth: int = 0,
    overrides: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    resolve_model: Callable[[str], str] | None = None,
) -> Resolved:
    """Everything ``agent_id`` gets, with provenance on every value.

    ``pool`` is the session pool: every tool this install has, minus the ones
    whose plugin is disabled. ``depth`` is the depth of the *spawner*, so 0
    means "this agent is being spawned by the orchestrator" -- which is where
    the pool carve-out applies.

    Never raises.
    """
    problems: list[Problem] = []
    chain: dict[str, tuple[Provenance, ...]] = {}
    overrides = overrides or {}

    # -- identity ---------------------------------------------------------
    is_orchestrator = agent_id == ORCHESTRATOR_ID
    defn = defs.get(agent_id)
    if is_orchestrator:
        role: Role = "orchestrator"
    elif defn is None:
        available = ", ".join(sorted(k for k in defs if k != ORCHESTRATOR_ID))
        return Resolved(
            id=agent_id,
            role="subagent",
            problems=(Problem(
                code="unknown_agent",
                severity="error",
                message=(f"unknown agent_type '{agent_id}'. Available: "
                         f"{available or 'none in this preset'}"),
                fix="Name one of the available agents, or add a definition for it.",
                subject=agent_id,
                field="agent_type",
            ),),
        )
    else:
        role = "orchestrator" if getattr(defn, "role", "subagent") == "orchestrator" \
            else "subagent"

    # -- layers -----------------------------------------------------------
    layers: list[_Layer] = [
        _Layer("default", "manifest.py", "",
               Composition().with_fields(ceiling=Mode.yolo)),
    ]
    for name, path, entries in state_store.layer_states(cwd):
        bodies = _prompt_bodies(entries)
        settings = _plugin_settings(entries)
        if not bodies and not settings:
            continue
        updates: dict[str, Any] = {}
        if bodies:
            updates["section_bodies"] = bodies
        if settings:
            updates["settings"] = settings
        layers.append(_Layer(name, path.name, str(path),  # type: ignore[arg-type]
                             Composition().with_fields(**updates)))

    preset_id = getattr(preset, "id", "")
    preset_comp = (
        getattr(preset, "orchestrator", Composition()) if is_orchestrator
        else getattr(preset, "agents", {}).get(agent_id, Composition())
    )
    preset_settings = getattr(preset, "settings", {}) or {}
    if preset_settings and not preset_comp.states("settings"):
        preset_comp = preset_comp.with_fields(
            settings={k: dict(v) for k, v in preset_settings.items()}
        )
    preset_comp, revoked, _unreached = _binding_contributions(
        getattr(preset, "bindings", ()) or (), agent_id, role, preset_comp
    )
    preset_layer = _Layer("preset", f"preset:{preset_id}", "", preset_comp)

    agent_layers: list[_Layer] = []
    if defn is not None:
        source = getattr(defn, "source", "internal")
        origin = getattr(defn, "path", "") or f"{source}:{agent_id}"
        agent_comp = getattr(defn, "composition", Composition())
        for comp in _expand_bases(agent_comp, defs):
            agent_layers.append(_Layer("agent", origin, getattr(defn, "path", ""), comp))

    # A base is layer-4 input, so it sits under the preset's own agent block
    # only when the preset does not state the field -- which the intersection
    # already handles, since order is irrelevant for capabilities.
    layers.extend(agent_layers)
    layers.append(preset_layer)
    if session:
        layers.append(_Layer("session", "session meta", "",
                             Composition.from_dict(session)))
    call_comp = Composition()
    if isinstance(overrides.get("model"), str) and overrides["model"]:
        call_comp = call_comp.with_fields(model=overrides["model"])
    if call_comp.explicit:
        layers.append(_Layer("call", "agent tool", "", call_comp))

    # -- the candidate pool ----------------------------------------------
    pool_names = [getattr(t, "name", "") for t in pool]
    pool_names = [n for n in pool_names if n]
    selectable = [
        n for n in pool_names
        if n not in DELEGATION_TOOLS and (role == "orchestrator" or n != "plan")
    ]

    # -- tools ------------------------------------------------------------
    asked, tool_chains, empty_patterns, literals = _intersect_named(
        layers, "tools", selectable
    )
    for pattern in revoked:
        for name in [n for n in asked if _matches(pattern, n)]:
            asked.discard(name)
            tool_chains[name].append(
                preset_layer.prov(rule=pattern, note="revoked by a binding")
            )

    if depth == 0 or parent is None:
        # The carve-out. The orchestrator's *grant* and the session's *pool*
        # are different sets: restricting what the orchestrator does with its
        # own hands is not a statement about the session's capability
        # envelope. Without this, "delegate everything" would be
        # indistinguishable from "this session cannot edit files". To restrict
        # the session, disable the plugin.
        parent_tools = set(selectable)
        parent_note = "session pool (depth 0)"
    else:
        parent_tools = set(parent.tools)
        parent_note = f"parent {parent.id}"

    granted = {n for n in asked if n in parent_tools}
    for name in sorted(asked - granted):
        tool_chains[name].append(
            Provenance(layer="parent", source=parent_note, rule=name,
                       note="withheld by the spawning agent")
        )
        if name in literals:
            problems.append(Problem(
                code="tool_withheld_by_parent",
                severity="error",
                message=(f"'{agent_id}' asks for the tool '{name}', which the "
                         f"spawning agent was not granted."),
                fix=("Grant it to the parent as well, or drop it from this "
                     "agent's tools."),
                subject=agent_id, field="tools",
                provenance=Provenance(layer="parent", source=parent_note, rule=name),
            ))

    for layer, pattern in empty_patterns:
        installed = pattern in pool_names
        problems.append(Problem(
            code="tool_not_installed" if not installed else "pattern_matched_nothing",
            severity="warning",
            message=(f"'{pattern}' matches no tool in this session."
                     if not installed else
                     f"'{pattern}' matched nothing selectable for '{agent_id}'."),
            fix=("Check the name, or ignore this if the tool comes from an MCP "
                 "server that is not connected here."),
            subject=agent_id, field="tools", provenance=layer.prov(rule=pattern),
        ))

    # -- spawns -----------------------------------------------------------
    agent_ids = [k for k in sorted(defs) if k != ORCHESTRATOR_ID]
    spawn_asked, spawn_chains, spawn_empty, spawn_literals = _intersect_named(
        layers, "spawns", agent_ids
    )
    if parent is not None:
        allowed_by_parent = set(parent.spawns)
        for name in sorted(spawn_asked - allowed_by_parent):
            spawn_chains[name].append(
                Provenance(layer="parent", source=f"parent {parent.id}", rule=name,
                           note="the spawning agent may not spawn this")
            )
            if name in spawn_literals:
                problems.append(Problem(
                    code="spawn_withheld_by_parent",
                    severity="error",
                    message=(f"'{agent_id}' may not be given '{name}' to spawn: "
                             f"'{parent.id}' may not spawn it either."),
                    fix="Grant it to the parent as well, or drop it here.",
                    subject=agent_id, field="spawns",
                    provenance=Provenance(layer="parent", source=parent.id, rule=name),
                ))
        spawn_asked &= allowed_by_parent

    child_depth = depth if is_orchestrator else depth + 1
    if not is_orchestrator and child_depth >= max_depth:
        for name in sorted(spawn_asked):
            spawn_chains[name].append(
                Provenance(layer="runtime", source="runtime.subagents", rule=name,
                           note=f"depth {child_depth} >= max_depth {max_depth}")
            )
        spawn_asked = set()

    for layer, pattern in spawn_empty:
        problems.append(Problem(
            code="unknown_agent_ref", severity="warning",
            message=f"'{pattern}' names no agent definition on this machine.",
            fix="Check the name, or ignore this if the preset is shared.",
            subject=agent_id, field="spawns", provenance=layer.prov(rule=pattern),
        ))

    spawns = tuple(name for name in agent_ids if name in spawn_asked)

    # -- the delegation pair, granted by depth, never by allowlist --------
    delegation = tuple(n for n in DELEGATION_TOOLS if n in pool_names) if spawns else ()
    for name in delegation:
        tool_chains.setdefault(name, []).append(
            Provenance(layer="runtime", source="tools/registry.py", rule="by depth",
                       note="the delegation pair is granted by depth")
        )
    tools = tuple(n for n in pool_names if n in granted) + delegation
    denied = tuple(n for n in pool_names if n not in tools)

    for name in tools:
        chain[f"tools.{name}"] = tuple(tool_chains.get(name, ()))
    for name in spawns:
        chain[f"spawns.{name}"] = tuple(spawn_chains.get(name, ()))

    # -- models -----------------------------------------------------------
    models, model_chain = _intersect_models(layers)
    if parent is not None and parent.models:
        if not models:
            models = tuple(parent.models)
        else:
            models = tuple(p for p in models if _admits(parent.models, p)) or tuple(
                p for p in parent.models if _admits(models, p)
            )
        model_chain.append(Provenance(layer="parent", source=parent.id,
                                      rule=", ".join(parent.models)))
    if model_chain:
        chain["models"] = tuple(model_chain)

    # -- ceiling ----------------------------------------------------------
    ceiling = Mode.yolo
    ceiling_chain: list[Provenance] = []
    for layer in layers:
        if not layer.comp.states("ceiling"):
            continue
        ceiling = narrower_mode(ceiling, layer.comp.ceiling)
        ceiling_chain.append(layer.prov(rule=layer.comp.ceiling.value))
    if parent is not None:
        capped = narrower_mode(ceiling, parent.ceiling)
        if capped != ceiling:
            problems.append(Problem(
                code="ceiling_capped", severity="warning",
                message=(f"'{agent_id}' asks for a ceiling of {ceiling.value} but "
                         f"'{parent.id}' is capped at {parent.ceiling.value}."),
                fix="Nothing to do: the narrower of the two applies.",
                subject=agent_id, field="ceiling",
            ))
            ceiling_chain.append(Provenance(
                layer="parent", source=parent.id, rule=parent.ceiling.value,
                note=f"capped from {ceiling.value}",
            ))
            ceiling = capped
    chain["ceiling"] = tuple(ceiling_chain) or (
        Provenance(layer="default", source="manifest.py", rule=ceiling.value),
    )

    # -- value fields, last writer wins -----------------------------------
    def _last(field_name: str, seed: Any) -> tuple[Any, list[Provenance]]:
        value, entries = seed, []
        for layer in layers:
            if layer.comp.states(field_name):
                value = getattr(layer.comp, field_name)
                entries.append(layer.prov(rule=str(value)))
        return value, entries

    model, model_prov = _last("model", "" if is_orchestrator else "worker")
    selectable_model, sel_prov = _last("model_selectable", True)
    max_turns, turns_prov = _last("max_turns", 30)
    if model_prov:
        chain["model"] = tuple(model_prov)
    if sel_prov:
        chain["model_selectable"] = tuple(sel_prov)
    if turns_prov:
        chain["max_turns"] = tuple(turns_prov)

    if overrides.get("model") and not selectable_model:
        problems.append(Problem(
            code="model_not_selectable", severity="error",
            message=(f"agent '{agent_id}' is pinned to {model} and does not "
                     "accept a model override"),
            fix="Spawn it without a model, or make the definition selectable.",
            subject=agent_id, field="model",
        ))
    if models and model and resolve_model is not None:
        try:
            slug = resolve_model(model)
        except Exception:  # a broken profile must not break resolution
            slug = model
        if not (_admits(models, model) or _admits(models, slug)):
            problems.append(Problem(
                code="model_outside_set", severity="error",
                message=(f"agent '{agent_id}' may only run on: "
                         f"{', '.join(models)} (asked for {model})"),
                fix="Pick a model the agent's policy admits.",
                subject=agent_id, field="model",
            ))

    # -- sections and bodies (never intersected: not capabilities) --------
    sections: tuple[str, ...] = ()
    for layer in layers:
        if layer.comp.states("sections") and layer.comp.sections is not None:
            sections = tuple(layer.comp.sections)
    bodies: dict[str, str] = {}
    for layer in layers:
        for section_id, body in layer.comp.section_bodies.items():
            bodies[section_id] = body
            chain.setdefault(f"section_bodies.{section_id}", ())
            chain[f"section_bodies.{section_id}"] += (layer.prov(rule="body"),)

    settings: dict[str, dict[str, Any]] = {}
    for layer in layers:
        for plugin_id, values in layer.comp.settings.items():
            slot = settings.setdefault(plugin_id, {})
            for key, value in values.items():
                slot[key] = value
                path = f"settings.{plugin_id}.{key}"
                chain[path] = chain.get(path, ()) + (layer.prov(rule=key),)

    colour, _ = _last("color", "cyan")
    skip, _ = _last("skip_project_instructions", False)
    if skip:
        chain["skip_project_instructions"] = (
            Provenance(layer="agent", source=agent_id, rule="true"),
        )
    chain["color"] = (Provenance(layer="agent", source=agent_id, rule=str(colour)),)

    problems.extend(state_store.local_settings_problems(cwd))

    return Resolved(
        id=agent_id,
        role=role,
        tools=tools,
        denied_tools=denied,
        spawns=spawns,
        sections=sections,
        section_bodies=bodies,
        models=models,
        model=model,
        model_selectable=bool(selectable_model),
        ceiling=ceiling,
        max_turns=int(max_turns),
        settings=settings,
        chain=chain,
        problems=tuple(problems),
    )


# --------------------------------------------------------------------------
# the session pool
# --------------------------------------------------------------------------

def session_pool(cwd: Path | None, tools: Iterable[Any]) -> list[Any]:
    """The tools this session has, after the session-wide revoke.

    ``plugins.<id>.enabled = false`` removes a plugin from the pool entirely --
    every agent, every depth. That toggle is the whole authoring surface for
    the revoke, and until this function existed it was decoration: the UI wrote
    the flag and nothing on the tool path ever read it.
    """
    disabled = state_store.disabled_plugin_ids(cwd)
    if not disabled:
        return list(tools)
    return [t for t in tools if f"tool.{getattr(t, 'name', '')}" not in disabled]


def default_mode(cwd: Path | None, fallback: str = "ask") -> str:
    """The starting permission mode, resolved once.

    It used to exist twice -- ``Config.default_mode`` in
    ``~/.quickcode/config.json`` and the ``runtime.permissions.default_mode``
    plugin setting -- with only the first consumed, so the knob the Settings UI
    renders did nothing. The plugin setting wins; the config value is the
    fallback.
    """
    value = state_store.plugin_setting(cwd, "runtime.permissions", "default_mode")
    if isinstance(value, str) and value.strip():
        try:
            return Mode(value.strip()).value
        except ValueError:
            pass
    return fallback
