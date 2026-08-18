"""Composition: what is attached to one agent, and what it ends up with.

Three types and one distinction.

``Composition`` is *authored*: the patterns, bodies and limits someone wrote
down for one agent. ``Resolved`` is *derived*: the concrete answer for one
agent in one session, with a provenance chain on every value. ``Binding`` is
the diffable one-row-per-statement form of "this plugin is attached to those
agents", and it is sugar -- the resolver desugars each into contributions to
compositions before resolution runs.

The distinction that makes the resolver order-independent lives here too:

* **Capability fields** -- ``tools``, ``spawns``, ``models``, ``ceiling`` --
  combine by **intersection**. Intersection is commutative and associative, so
  "which layer wins" is not a question that can be asked about them. This is
  the narrowing invariant, and it is why no layer, definition or argument can
  widen a child.
* **Value fields** -- ``model``, ``max_turns``, ``section_bodies``,
  ``settings``, ``model_selectable``, ``color``, ``skip_project_instructions``
  -- combine by **last writer wins** down the ordered layer list.
* ``model`` is the one hybrid: a value field that must be a member of the
  intersected ``models`` set.

Prompt sections are deliberately *not* intersected. They are not capabilities;
adding one grants nothing, and a child legitimately needs sections its parent
lacks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from quickcode.core.permissions import Mode
from quickcode.kernel.problems import Problem, Provenance

Role = Literal["orchestrator", "subagent"]

# The reserved id for the session's main agent. The leading "@" is why no
# ordinary agent name can collide with it.
ORCHESTRATOR_ID = "@orchestrator"

# The delegation set is granted by depth, never by allowlist
# (tools/registry.py). Naming it here keeps the resolver honest about which
# tools it is allowed to have an opinion on. Four, not two: an agent that may
# start a detached job must be able to ask after it and collect it, and a
# preset that granted the spawn tool but not the collectors would strand
# every background report it produced.
DELEGATION_TOOLS = ("agent", "send_message", "agent_status", "agent_result")

# plan < ask < auto-edit < dontask < yolo (least -> most privileged).
MODE_PRIVILEGE: dict[Mode, int] = {
    Mode.plan: 0, Mode.ask: 1, Mode.auto_edit: 2, Mode.dontask: 3, Mode.yolo: 4,
}


def cap_mode(parent: Mode, cap: Mode) -> Mode:
    """effective = min(parent, cap); plan collapses to ask.

    Headless children do not do the interactive plan-review dance, so a plan
    ceiling on a subagent means "ask", not "cannot act at all".
    """
    eff = parent if MODE_PRIVILEGE[parent] <= MODE_PRIVILEGE[cap] else cap
    return Mode.ask if eff == Mode.plan else eff


def narrower_mode(a: Mode, b: Mode) -> Mode:
    """The less privileged of two ceilings. Intersection for ``ceiling``."""
    return a if MODE_PRIVILEGE[a] <= MODE_PRIVILEGE[b] else b


def parse_mode(raw: Any, default: Mode = Mode.ask) -> Mode:
    if isinstance(raw, Mode):
        return raw
    try:
        return Mode(str(raw))
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------
# selectors
# --------------------------------------------------------------------------

SELECTORS = ("@orchestrator", "@subagents", "@all")


def selector_matches(selector: str, agent_id: str, role: Role) -> bool:
    """Do the four surviving selectors reach this agent?

    ``@session`` and ``agents:<a>,<b>`` are deliberately absent: the first is
    ``plugins.<id>.enabled`` (pool admission, not attachment) and the second is
    two bindings that say the same thing with better provenance.
    """
    sel = (selector or "").strip()
    if sel == "@all":
        return True
    if sel == "@orchestrator":
        return role == "orchestrator"
    if sel == "@subagents":
        return role == "subagent"
    if sel.startswith("agent:"):
        return sel[len("agent:"):].strip() == agent_id
    return False


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

# Which fields intersect and which overwrite. The resolver reads these rather
# than restating them, so the two can never drift.
CAPABILITY_FIELDS = ("tools", "spawns", "models", "ceiling")
VALUE_FIELDS = (
    "model", "model_selectable", "max_turns", "color",
    "skip_project_instructions", "base",
)


@dataclass(frozen=True)
class Composition:
    """What is attached to one agent. Authored; never the resolved answer."""

    # Patterns, resolved against the live pool. None = inherit.
    tools: tuple[str, ...] | None = None
    # Agent ids or globs this agent may spawn. None = inherit.
    spawns: tuple[str, ...] | None = None
    # Prompt section ids. None = the default set for the role.
    sections: tuple[str, ...] | None = None
    # Section id -> replacement body.
    section_bodies: dict[str, str] = field(default_factory=dict)
    # Allowed model set: roles, slugs or globs. Empty = any.
    models: tuple[str, ...] = ()
    # The default pick within that set.
    model: str = ""
    model_selectable: bool = True
    ceiling: Mode = Mode.ask
    # Role-conditional: meaningless on @orchestrator, a delegation budget on a
    # subagent.
    max_turns: int = 30
    color: str = "cyan"
    skip_project_instructions: bool = False
    # plugin id -> {key: value}.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Another composition to start from.
    base: str = ""
    # Which fields this composition actually states. A layer that says nothing
    # about ``max_turns`` must not overwrite a lower layer with the dataclass
    # default, so "unset" and "set to the default value" have to be different
    # facts. Never part of the resolved answer; purely a layering aid.
    explicit: frozenset[str] = field(default_factory=frozenset)

    def states(self, name: str) -> bool:
        return name in self.explicit

    def with_fields(self, **kw: Any) -> Composition:
        """A copy with these fields set *and* marked as explicitly stated."""
        return replace(self, explicit=self.explicit | set(kw), **kw)

    def to_dict(self) -> dict[str, Any]:
        """The on-disk shape: only what this composition actually states."""
        out: dict[str, Any] = {}
        for name in ("tools", "spawns", "sections"):
            if self.states(name):
                value = getattr(self, name)
                out[name] = None if value is None else list(value)
        if self.states("models"):
            out["models"] = list(self.models)
        if self.states("section_bodies"):
            out["section_bodies"] = dict(self.section_bodies)
        if self.states("settings"):
            out["settings"] = {k: dict(v) for k, v in self.settings.items()}
        if self.states("ceiling"):
            out["ceiling"] = self.ceiling.value
        for name in VALUE_FIELDS:
            if self.states(name):
                out[name] = getattr(self, name)
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Composition:
        """Tolerant parse. A malformed key is dropped, never raised over.

        Resolution is total (a session must always open), so the parse it
        depends on has to be total too.
        """
        if not isinstance(raw, dict):
            return cls()
        stated: dict[str, Any] = {}

        for name in ("tools", "spawns", "sections"):
            if name in raw:
                value = raw.get(name)
                if value is None:
                    stated[name] = None
                elif isinstance(value, (list, tuple)):
                    stated[name] = tuple(str(v) for v in value if str(v).strip())
        if "models" in raw and isinstance(raw["models"], (list, tuple)):
            stated["models"] = tuple(str(v) for v in raw["models"] if str(v).strip())
        if isinstance(raw.get("section_bodies"), dict):
            stated["section_bodies"] = {
                str(k): str(v) for k, v in raw["section_bodies"].items()
                if isinstance(v, str)
            }
        if isinstance(raw.get("settings"), dict):
            stated["settings"] = {
                str(k): dict(v) for k, v in raw["settings"].items()
                if isinstance(v, dict)
            }
        if "ceiling" in raw:
            stated["ceiling"] = parse_mode(raw.get("ceiling"))
        if "model" in raw and isinstance(raw["model"], str):
            stated["model"] = raw["model"]
        if "model_selectable" in raw:
            stated["model_selectable"] = bool(raw["model_selectable"])
        if "max_turns" in raw:
            try:
                stated["max_turns"] = max(1, int(raw["max_turns"]))
            except (TypeError, ValueError):
                pass
        if "color" in raw and isinstance(raw["color"], str):
            stated["color"] = raw["color"]
        if "skip_project_instructions" in raw:
            stated["skip_project_instructions"] = bool(raw["skip_project_instructions"])
        if "base" in raw and isinstance(raw["base"], str):
            stated["base"] = raw["base"]

        return cls(**stated, explicit=frozenset(stated))


EMPTY = Composition()


# --------------------------------------------------------------------------
# RuntimeLimits
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeLimits:
    """The runtime's numbers, resolved once per session and then frozen.

    These were declared in the manifest, rendered by the Settings UI,
    written to disk -- and read by nobody: the loop, the compactor and the
    spawner each used a module constant instead. They live here, next to
    ``Resolved``, because they are the same kind of thing: a derived answer a
    session is handed at open and keeps for its whole life. A settings edit
    while a conversation is running must not change how that conversation
    behaves mid-turn, so nothing re-reads them.

    The values below are the fallbacks used when nothing has been configured
    and no manifest is reachable. The declared defaults, and the minima and
    maxima that clamp a configured value, live in ``kernel/manifest.py`` --
    ``resolve.runtime_limits`` reads them from there rather than restating
    them here.
    """

    max_rounds: int = 50
    compaction_enabled: bool = True
    compaction_threshold: float = 0.8
    keep_turns: int = 2
    max_depth: int = 2
    max_agents: int = 50
    # How many detached subagent jobs may run at the same time. ``max_agents``
    # is a lifetime total and says nothing about simultaneity, which only
    # became reachable when spawning stopped blocking the turn.
    max_parallel: int = 4


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------

Effect = Literal["grant", "revoke", "set"]


@dataclass(frozen=True)
class Binding:
    """One authored statement contributing to one or more compositions.

    "narrow" is not an effect: capability fields intersect unconditionally, so
    narrowing is what ``grant`` already does at every layer below the one that
    holds the smaller set.
    """

    plugin: str          # "tool.bash", "prompt.verification", "agent.explore"
    to: str              # a selector
    effect: Effect = "grant"
    value: Any = None    # effect-dependent: a section body, a settings dict

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"plugin": self.plugin, "to": self.to,
                               "effect": self.effect}
        if self.value is not None:
            out["value"] = self.value
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Binding | None:
        if not isinstance(raw, dict):
            return None
        plugin = str(raw.get("plugin", "")).strip()
        to = str(raw.get("to", "")).strip()
        if not plugin or not to:
            return None
        effect = raw.get("effect", "grant")
        if effect not in ("grant", "revoke", "set"):
            effect = "grant"
        return cls(plugin=plugin, to=to, effect=effect, value=raw.get("value"))


# --------------------------------------------------------------------------
# Resolved
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolved:
    """What an agent actually has. Derived; stored only as a session snapshot."""

    id: str
    role: Role
    # Concrete tool names, in pool order, not patterns.
    tools: tuple[str, ...] = ()
    # In the pool, not granted. Listed, never omitted: "why doesn't my agent
    # have write" is the question people actually ask, and an omitted key
    # answers nothing.
    denied_tools: tuple[str, ...] = ()
    spawns: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    section_bodies: dict[str, str] = field(default_factory=dict)
    models: tuple[str, ...] = ()
    model: str = ""
    model_selectable: bool = True
    ceiling: Mode = Mode.ask
    max_turns: int = 30
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Dotted path -> the layers that touched it, last one winning.
    # "tools.write", "model", "ceiling", "settings.runtime.agent_loop.max_rounds".
    chain: dict[str, tuple[Provenance, ...]] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()

    # -- problems ---------------------------------------------------------

    def errors(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.severity == "error")

    def advisories(self) -> tuple[Problem, ...]:
        return tuple(p for p in self.problems if p.severity != "error")

    def refusal(self) -> str:
        """One message naming every error, for a spawn that must not happen."""
        return "; ".join(p.message for p in self.errors())

    # -- serialization ----------------------------------------------------

    def values_json(self) -> dict[str, Any]:
        """Just the answer -- no provenance, no problems. What the digest is
        taken over, so re-explaining a value is not drift."""
        return {
            "id": self.id,
            "role": self.role,
            "tools": list(self.tools),
            "denied_tools": list(self.denied_tools),
            "spawns": list(self.spawns),
            "sections": list(self.sections),
            "section_bodies": dict(self.section_bodies),
            "models": list(self.models),
            "model": self.model,
            "model_selectable": self.model_selectable,
            "ceiling": self.ceiling.value,
            "max_turns": self.max_turns,
            "settings": {k: dict(v) for k, v in self.settings.items()},
        }

    def digest(self) -> str:
        canonical = json.dumps(self.values_json(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        out = self.values_json()
        out["chain"] = {
            key: [p.to_json() for p in entries]
            for key, entries in sorted(self.chain.items())
        }
        out["problems"] = [p.to_json() for p in self.problems]
        out["digest"] = self.digest()
        return out

    @classmethod
    def from_json(cls, raw: Any) -> Resolved | None:
        """Rebuild a frozen composition off a session meta record.

        Returns None rather than raising for anything unusable: a session whose
        snapshot cannot be read must fall back to re-resolving, not fail to
        open.
        """
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        role = raw.get("role")
        chain: dict[str, tuple[Provenance, ...]] = {}
        for key, entries in (raw.get("chain") or {}).items():
            if isinstance(entries, list):
                chain[str(key)] = tuple(
                    Provenance.from_json(e) for e in entries if isinstance(e, dict)
                )
        return cls(
            id=str(raw["id"]),
            role="orchestrator" if role == "orchestrator" else "subagent",
            tools=tuple(str(t) for t in raw.get("tools", [])),
            denied_tools=tuple(str(t) for t in raw.get("denied_tools", [])),
            spawns=tuple(str(t) for t in raw.get("spawns", [])),
            sections=tuple(str(t) for t in raw.get("sections", [])),
            section_bodies={str(k): str(v)
                            for k, v in (raw.get("section_bodies") or {}).items()},
            models=tuple(str(t) for t in raw.get("models", [])),
            model=str(raw.get("model", "")),
            model_selectable=bool(raw.get("model_selectable", True)),
            ceiling=parse_mode(raw.get("ceiling")),
            max_turns=int(raw.get("max_turns", 30) or 30),
            settings={str(k): dict(v)
                      for k, v in (raw.get("settings") or {}).items()
                      if isinstance(v, dict)},
            chain=chain,
            problems=tuple(
                Problem.from_json(p) for p in raw.get("problems", [])
                if isinstance(p, dict)
            ),
        )
