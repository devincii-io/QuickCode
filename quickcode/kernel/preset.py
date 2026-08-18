"""Presets: the plugin composition one session's agents run.

A preset is not a settings profile. It is the answer to "which agent am I
talking to" -- which tools it has, which subagents it may spawn, what its
prompt says, and how much it may do without asking. Sessions record the
preset they started with and keep it: changing the preset mid-flight would
change the tools under a conversation that has already been told what it has.

A preset is now a *named set*: the orchestrator's ``Composition``, the
compositions available to spawn, and the bindings that contribute to both. The
legacy flat shape (``tools``/``agents``/``prompt_overrides``/``default_mode``)
still parses and still means exactly what it meant -- ``from_dict`` lifts it
onto the orchestrator, discriminating the two meanings of ``agents`` by type: a
list is the legacy spawn list, a dict is the new per-agent compositions.

Built-ins are defined here; user presets live in ``~/.quickcode/settings.json``
and ``<cwd>/.quickcode/settings.json`` under ``presets`` and are the same
shape, the project file shadowing the user one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quickcode.kernel.composition import Binding, Composition
from quickcode.kernel.state import _read, project_settings_path, user_settings_path
from quickcode.security.trust import GATED_PRESET_FIELDS, project_may_state

log = logging.getLogger("quickcode.kernel.preset")

PRESETS_KEY = "presets"
ACTIVE_KEY = "active_preset"
DEFAULT_PRESET = "standard"

# Preset fields a project may only state once it is trusted -- the gate's own
# list, imported rather than restated so the two cannot drift apart.
_GATED_FIELDS = GATED_PRESET_FIELDS


@dataclass(frozen=True)
class Preset:
    id: str
    title: str
    description: str = ""
    builtin: bool = False
    base: str = ""
    # The session's main agent, under the reserved id "@orchestrator".
    orchestrator: Composition = field(default_factory=Composition)
    # Per-agent composition overlays, keyed by agent id. Empty is normal: an
    # agent with no entry here runs its definition unchanged.
    agents: dict[str, Composition] = field(default_factory=dict)
    # Sugar over composition edits, kept as a type because it is the diffable
    # one-row-per-statement form and because "@subagents" cannot be expressed
    # by enumerating agents that may not exist on another machine.
    bindings: tuple[Binding, ...] = ()
    # plugin id -> {setting: value}, applied while this preset is active.
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The session's *starting* permission mode. Not the ceiling: the ceiling is
    # ``orchestrator.ceiling`` and caps what the mode may ever be raised to,
    # while this is where the session begins. Conflating them would lock every
    # existing preset out of Shift+Tab.
    default_mode: str = ""

    # -- legacy read-through views ---------------------------------------
    # Kept so ``select_tools`` and the current settings UI keep working through
    # the transition. Deleted once nothing calls them.

    @property
    def tools(self) -> tuple[str, ...]:
        return self.orchestrator.tools if self.orchestrator.tools is not None else ("*",)

    @property
    def spawns(self) -> tuple[str, ...]:
        spawns = self.orchestrator.spawns
        return spawns if spawns is not None else ("*",)

    @property
    def prompt_overrides(self) -> dict[str, str]:
        return dict(self.orchestrator.section_bodies)

    def to_dict(self) -> dict[str, Any]:
        """The on-disk body.

        Both shapes are written for one release: an older build reading this
        file loses ``orchestrator``/``bindings`` and falls back to the legacy
        keys, which leaves it with a degraded but valid composition rather than
        a broken one. ``agents`` can only carry one of the two meanings, so it
        is the dict when there are per-agent compositions and the legacy spawn
        list otherwise.
        """
        body: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "builtin": self.builtin,
            "base": self.base,
            # ``agents`` carries two meanings by type, so the spawn list also
            # gets an unambiguous key of its own. Readers should prefer it.
            "spawns": list(self.spawns),
            # legacy
            "tools": list(self.tools),
            "prompt_overrides": dict(self.prompt_overrides),
            "default_mode": self.default_mode,
            "settings": {k: dict(v) for k, v in self.settings.items()},
            # new
            "orchestrator": self.orchestrator.to_dict(),
        }
        if self.agents:
            body["agents"] = {k: v.to_dict() for k, v in self.agents.items()}
        else:
            body["agents"] = list(self.spawns)
        if self.bindings:
            body["bindings"] = [b.to_dict() for b in self.bindings]
        return body

    @classmethod
    def from_dict(cls, preset_id: str, raw: dict[str, Any],
                  *, base: Preset | None = None) -> Preset:
        """Build a preset, inheriting anything unspecified from ``base``.

        Never raises: a hand-edited settings file must degrade, not take the
        app down.
        """
        src = base or cls(id=preset_id, title=preset_id)
        raw = raw if isinstance(raw, dict) else {}

        agents_raw = raw.get("agents")
        agents: dict[str, Composition] = dict(src.agents)
        legacy_spawns: tuple[str, ...] | None = None
        if isinstance(agents_raw, dict):
            agents = {str(k): Composition.from_dict(v) for k, v in agents_raw.items()}
        elif isinstance(agents_raw, (list, tuple)):
            legacy_spawns = tuple(str(a) for a in agents_raw if str(a).strip())
            if not agents_raw:
                legacy_spawns = ()

        orch = Composition.from_dict(raw.get("orchestrator"))
        # Legacy lift, per the migration table. Each key only applies when the
        # new block does not already state that field, so a file carrying both
        # shapes (which is what save_preset writes) reads the new one.
        if "tools" in raw and not orch.states("tools") and isinstance(
            raw.get("tools"), (list, tuple)
        ):
            orch = orch.with_fields(
                tools=tuple(str(t) for t in raw["tools"] if str(t).strip())
            )
        if legacy_spawns is not None and not orch.states("spawns"):
            orch = orch.with_fields(spawns=legacy_spawns)
        if isinstance(raw.get("prompt_overrides"), dict) and not orch.states(
            "section_bodies"
        ):
            orch = orch.with_fields(section_bodies={
                str(k): str(v) for k, v in raw["prompt_overrides"].items()
                if isinstance(v, str)
            })
        if not orch.explicit:
            orch = src.orchestrator

        bindings: list[Binding] = []
        for item in raw.get("bindings") or []:
            parsed = Binding.from_dict(item)
            if parsed is not None:
                bindings.append(parsed)

        settings = raw.get("settings")
        return cls(
            id=preset_id,
            title=raw.get("title") or src.title or preset_id,
            description=raw.get("description", src.description),
            builtin=False,
            base=raw.get("base", ""),
            orchestrator=orch,
            agents=agents,
            bindings=tuple(bindings) or src.bindings,
            settings=dict(settings) if isinstance(settings, dict) else dict(src.settings),
            default_mode=raw.get("default_mode", src.default_mode),
        )


def builtin_presets() -> dict[str, Preset]:
    return {
        "standard": Preset(
            id="standard",
            title="Standard",
            description=(
                "The full coding agent: files, shell, the task board, planning "
                "and subagents."
            ),
            builtin=True,
        ),
        "minimal": Preset(
            id="minimal",
            title="Minimal",
            description=(
                "Read, write, edit and a shell. No task board, no planning, no "
                "delegation -- a small agent for a small job."
            ),
            builtin=True,
            orchestrator=Composition().with_fields(
                tools=("read", "write", "edit", "bash"), spawns=()
            ),
        ),
        "explore": Preset(
            id="explore",
            title="Explore",
            description=(
                "Read-only. Searches and explains, changes nothing, and cannot "
                "be talked into it because the tools are not there."
            ),
            builtin=True,
            orchestrator=Composition().with_fields(
                tools=("read", "glob", "grep"), spawns=("explore",)
            ),
            default_mode="plan",
        ),
    }


def _presets_from(raw: dict[str, Any], builtins: dict[str, Preset],
                  *, gated: bool = False) -> dict[str, Preset]:
    """Parse one layer's presets. ``gated`` drops what a project may not set.

    Everything else a preset states is intersected somewhere downstream --
    tools, spawns and models narrow, the ceiling narrows, section bodies are
    prompt text the model already reads untrusted. ``default_mode`` is the one
    field that can *raise* what the session may do on its own, so it is the one
    field the gate has an opinion about, and only when the value asks for more
    than ``ask``. Dropping just that leaves the rest of the preset working,
    which is what the composition is for.
    """
    section = raw.get(PRESETS_KEY)
    if not isinstance(section, dict):
        return {}
    out: dict[str, Preset] = {}
    for preset_id, body in section.items():
        if not isinstance(body, dict):
            continue
        refused = [f for f in _GATED_FIELDS
                   if body.get(f) and not project_may_state(f, body[f])] if gated else []
        if refused:
            log.warning("project is not trusted; ignoring %s on preset %r",
                        ", ".join(refused), preset_id)
            body = {k: v for k, v in body.items() if k not in refused}
        base = builtins.get(body.get("base", "")) or builtins.get(DEFAULT_PRESET)
        try:
            out[preset_id] = Preset.from_dict(preset_id, body, base=base)
        except Exception as exc:  # a malformed preset must not hide the rest
            log.warning("ignoring preset %r: %s", preset_id, exc)
    return out


def load_presets(cwd: Path | None, *, trusted: bool | None = None) -> dict[str, Preset]:
    """Built-ins, then user presets, then project presets (each shadows).

    Note the deliberate asymmetry with ``active_preset_id``, which reads
    project-then-user: the most specific file *names* the active preset, and
    the most specific file *wins* when defining one. Both are correct; the
    combination is only surprising if you meet it undocumented.
    """
    from quickcode.security import trust

    presets = builtin_presets()
    presets.update(_presets_from(_read(user_settings_path()), builtin_presets()))
    if cwd is not None:
        presets.update(_presets_from(
            _read(project_settings_path(cwd)), builtin_presets(),
            gated=not trust.resolve_trust(cwd, trusted),
        ))
    return presets


def active_preset_id(cwd: Path | None) -> str:
    for raw in ([_read(project_settings_path(cwd))] if cwd else []) + [
        _read(user_settings_path())
    ]:
        value = raw.get(ACTIVE_KEY)
        if isinstance(value, str) and value:
            return value
    return DEFAULT_PRESET


def resolve(cwd: Path | None, preset_id: str = "", *,
            trusted: bool | None = None) -> Preset:
    """The preset to run, falling back to the standard one if it is gone.

    A session that recorded a preset which has since been deleted must still
    open -- losing the composition is bad, refusing to show the conversation
    is worse.
    """
    presets = load_presets(cwd, trusted=trusted)
    wanted = preset_id or active_preset_id(cwd)
    preset = presets.get(wanted)
    if preset is None:
        if wanted:
            log.warning("preset %r not found; falling back to %s", wanted, DEFAULT_PRESET)
        preset = presets.get(DEFAULT_PRESET) or builtin_presets()[DEFAULT_PRESET]
    return preset


def save_preset(cwd: Path, preset: Preset) -> None:
    """Write a user preset into the project settings file."""
    path = project_settings_path(cwd)
    raw = _read(path)
    section = raw.get(PRESETS_KEY)
    if not isinstance(section, dict):
        section = {}
    body = preset.to_dict()
    body.pop("id", None)
    body.pop("builtin", None)
    section[preset.id] = body
    raw[PRESETS_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def set_active(cwd: Path, preset_id: str) -> None:
    path = project_settings_path(cwd)
    raw = _read(path)
    raw[ACTIVE_KEY] = preset_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def select_tools(preset: Preset, pool: list) -> list:
    """The tools a preset admits, out of what the session actually has.

    Superseded by ``kernel/resolve.resolve_composition`` for the session path;
    kept because it is the cheapest way to answer the same question without a
    pool of agent definitions, and because the settings UI still asks it.

    ``["*"]`` is not special-cased into "skip the filter": a glob against the
    live pool gives the same answer and keeps one code path.
    """
    from quickcode.tools.registry import select

    tools = select(pool, preset.tools)
    if not preset.spawns:
        # No subagents means the delegation tools would only ever fail. Read
        # off the one list, so a tool added to the set is covered here too.
        from quickcode.kernel.composition import DELEGATION_TOOLS

        tools = [t for t in tools if t.name not in DELEGATION_TOOLS]
    return tools
