"""The plugin registry: one place that knows what exists and how it is set.

Discovery has three sources, in a fixed order so a later one can extend but
never silently replace an earlier one:

1. ``manifest.py``  -- the internal plugins we ship.
2. entry points     -- third-party packages (``quickcode.tools`` and friends).
3. config           -- data-driven plugins, e.g. one per configured MCP server.

The registry holds specs plus persisted state. It does not build tools,
render prompts, or run anything -- the subsystems do that, asking the
registry what is enabled and what its settings are. Keeping it inert is what
makes it safe for the Settings UI to read on every request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import quote

from quickcode.kernel import state as state_store
from quickcode.kernel.problems import Problem, Provenance
from quickcode.kernel.spec import (
    Kind,
    LockedSetting,
    NeedsConfirmation,
    PluginSpec,
    Recourse,
    UnknownPlugin,
    UnknownSetting,
)

log = logging.getLogger("quickcode.kernel.registry")


def _recourse_json(recourse: Recourse | None) -> dict[str, str] | None:
    """A recourse is a button, so it crosses the wire as one or not at all."""
    if recourse is None or recourse.action == "none":
        return None
    return {"action": recourse.action, "label": recourse.label,
            "target": recourse.target}


@dataclass(frozen=True)
class Use:
    """One thing that would move if this plugin changed.

    ``kind`` is the *user's* vocabulary, not the spec's: a composition or an
    agent, because those are the two pages you would go and edit. ``href`` is
    the address of that page, so the block is a set of links rather than a set
    of names you then have to go and find.
    """

    kind: str          # "composition" | "agent"
    id: str            # preset id, or agent id ("explore")
    title: str
    via: str           # the sentence that says *how* it uses this plugin
    href: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "title": self.title,
                "via": self.via, "href": self.href}


def _matches(pattern: str, name: str) -> bool:
    return pattern == name or fnmatchcase(name, pattern)


def _is_glob(text: str) -> bool:
    return any(ch in text for ch in ("*", "?", "["))


def _mcp_server(tool_name: str) -> str:
    """``mcp__files__read`` -> ``files``; "" for anything that is not MCP."""
    if not tool_name.startswith("mcp__"):
        return ""
    rest = tool_name[len("mcp__"):]
    head, sep, _ = rest.partition("__")
    return head if sep else ""


_BINDING_VIA = {
    "grant": "a binding grants it to {to}",
    "revoke": "a binding revokes it from {to}",
    "set": "a binding sets its value for {to}",
}


def _sets_via(values: dict[str, Any] | None) -> str:
    keys = ", ".join(sorted(values)) if values else ""
    return f"it sets {keys}" if keys else "it sets values here"


class _PoolTool:
    """A stand-in for a real tool while resolving.

    ``resolve_composition`` only ever asks the pool for ``.name``, and the
    registry knows every installed tool by name already. Building the real
    tool objects again just to read their names back off would be the one
    thing this module promises not to do.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class PluginRegistry:
    """Specs + persisted state for one project."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = Path(cwd) if cwd else None
        self._specs: dict[str, PluginSpec] = {}
        self._state: dict[str, dict[str, Any]] = state_store.load_state(self.cwd)
        # One array, two views: validation problems and resolution conflicts
        # are the same thing at two different times -- something the user wrote
        # does not do what they think -- so they land in one place and render
        # through one card rather than growing a second endpoint each.
        self.problems: list[Problem] = []
        # The reverse index behind USED BY. Built once, lazily, and held on the
        # *instance* -- never on the module. ``_registry_for`` rebuilds the
        # registry on every request precisely so that an edit is visible on the
        # next one; a process-level cache here would reintroduce exactly the
        # staleness that deliberate rebuild exists to avoid. See ``used_by``.
        self._used_by: dict[str, list[Use]] | None = None

    def add_problem(self, problem: Problem) -> None:
        self.problems.append(problem)

    def add_problems(self, problems: list[Problem]) -> None:
        self.problems.extend(problems)

    # -- registration ----------------------------------------------------

    def register(self, spec: PluginSpec) -> None:
        if spec.id in self._specs:
            # Two plugins claiming one id would make the UI show a capability
            # the runtime does not have. Keep the first -- bootstrap registers
            # the internal specs before anything else, so this is where a
            # reserved-id collision loses -- and record it where the user can
            # see it. A warning in a log file is not a surface.
            kept = self._specs[spec.id]
            log.warning("duplicate plugin id %r from %s ignored", spec.id, spec.source)
            self.problems.append(Problem(
                code="id_duplicate", severity="error",
                message=(f"'{spec.id}' is claimed twice: the {kept.source} one "
                         f"is in use and the {spec.source} one was refused"),
                fix=("Rename the second one. An id names one plugin; letting a "
                     "later definition replace an earlier one would mean a "
                     "cloned repository could quietly stand in for something "
                     "you trust."),
                subject=spec.id,
                provenance=Provenance(layer="project", source=spec.source,
                                      path=spec.path),
            ))
            return
        self._specs[spec.id] = spec
        self._used_by = None  # the candidate sets changed

    def register_all(self, specs: list[PluginSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # -- reading ---------------------------------------------------------

    def get(self, plugin_id: str) -> PluginSpec:
        spec = self._specs.get(plugin_id)
        if spec is None:
            raise UnknownPlugin(f"no plugin {plugin_id!r}")
        return spec

    def all(self) -> list[PluginSpec]:
        return sorted(self._specs.values(), key=lambda s: (s.kind, s.group, s.title))

    def by_kind(self, kind: Kind) -> list[PluginSpec]:
        return [s for s in self.all() if s.kind == kind]

    def groups(self) -> dict[str, list[PluginSpec]]:
        out: dict[str, list[PluginSpec]] = {}
        for spec in self.all():
            out.setdefault(spec.group or spec.kind, []).append(spec)
        return out

    def is_enabled(self, plugin_id: str) -> bool:
        spec = self.get(plugin_id)
        if spec.required:
            return True
        entry = self._state.get(plugin_id, {})
        value = entry.get("enabled")
        return spec.enabled_by_default if value is None else bool(value)

    def enabled(self, kind: Kind | None = None) -> list[PluginSpec]:
        specs = self.by_kind(kind) if kind else self.all()
        return [s for s in specs if self.is_enabled(s.id)]

    def settings(self, plugin_id: str) -> dict[str, Any]:
        """Effective settings: declared defaults under persisted overrides."""
        spec = self.get(plugin_id)
        values = spec.defaults()
        saved = self._state.get(plugin_id, {}).get("settings")
        if isinstance(saved, dict):
            for key, raw in saved.items():
                setting = spec.setting(key)
                if setting is None:
                    continue  # a knob from another version; ignore, don't crash
                try:
                    values[key] = setting.coerce(raw)
                except (TypeError, ValueError) as exc:
                    log.warning("ignoring bad value for %s.%s: %s", plugin_id, key, exc)
        return values

    def setting(self, plugin_id: str, key: str, default: Any = None) -> Any:
        return self.settings(plugin_id).get(key, default)

    # -- traceability: what would move if this changed --------------------

    def used_by(self, plugin_id: str) -> list[Use]:
        """Every composition and agent definition that reaches this plugin.

        This is the answer to "if I change this, what moves", and it is the
        question the whole configuration view exists to ask. For ``tool.bash``
        it names every preset whose orchestrator ends up holding ``bash`` and
        every agent definition that lists it.

        **Cost and cache scope.** Answering it needs a resolve, and a naive
        implementation would run one per agent per plugin render -- quadratic
        over the Parts list. So the whole reverse index is computed once and
        every lookup after that is a dict hit. The cache lives on this
        instance and nowhere else: the server builds a fresh registry per
        request (it reads the settings files, and a Settings page showing a
        stale answer is worse than the file IO), so a process-level cache
        would answer with the composition you had *before* the edit you just
        made. Per-request is the only correct scope.

        Never raises: a preset that no longer parses costs you the block, not
        the page.
        """
        if self._used_by is None:
            try:
                self._used_by = self._build_used_by()
            except Exception as exc:  # a broken preset must not break a detail page
                log.warning("used_by index failed: %s", exc)
                self._used_by = {}
        return list(self._used_by.get(plugin_id, ()))

    def _build_used_by(self) -> dict[str, list[Use]]:
        from quickcode.kernel import preset as preset_module
        from quickcode.kernel.composition import DELEGATION_TOOLS, ORCHESTRATOR_ID
        from quickcode.kernel.resolve import resolve_composition

        index: dict[str, dict[tuple[str, str], Use]] = {}

        def add(target: str, use: Use) -> None:
            if target not in self._specs:
                return  # a name that resolves to no plugin here is not a link
            slot = index.setdefault(target, {})
            slot.setdefault((use.kind, use.id), use)

        tool_names = [s.id[len("tool."):] for s in self.by_kind("tool")]
        agent_ids = [s.id[len("agent."):] for s in self.by_kind("agent")]
        defs = self._agent_defs()
        pool = [_PoolTool(name) for name in tool_names]

        def composition_use(preset: Any, via: str) -> Use:
            return Use(kind="composition", id=preset.id,
                       title=preset.title or preset.id, via=via,
                       href=f"#/config/compositions/{quote(preset.id, safe='')}")

        def agent_use(name: str, via: str) -> Use:
            return Use(kind="agent", id=name, title=name, via=via,
                       href=f"#/config/agents/{quote('agent.' + name, safe='')}")

        # -- compositions: one resolve each, for the orchestrator ----------
        # The orchestrator's resolved tool list is the only honest answer to
        # "does this composition grant bash": it is what survives the preset's
        # own block, its base, its bindings and its revokes.
        presets = preset_module.load_presets(self.cwd)
        for preset in sorted(presets.values(), key=lambda p: p.id):
            resolved = resolve_composition(
                ORCHESTRATOR_ID, pool=pool, preset=preset, defs=defs, cwd=self.cwd,
            )
            servers: set[str] = set()
            for name in resolved.tools:
                via = ("granted by depth, because it can spawn"
                       if name in DELEGATION_TOOLS
                       else "its orchestrator holds it")
                add(f"tool.{name}", composition_use(preset, via))
                server = _mcp_server(name)
                if server:
                    servers.add(server)
            for server in sorted(servers):
                add(f"mcp.{server}", composition_use(
                    preset, "its orchestrator holds tools from this server"))
            for name in resolved.spawns:
                add(f"agent.{name}", composition_use(
                    preset, "its orchestrator may spawn it"))

            # Sections, bodies and settings come off the preset itself rather
            # than off ``resolved``: the resolved values fold in the user and
            # project settings layers too, and attributing those to a preset
            # that never mentioned them would be a lie in a link.
            orch = preset.orchestrator
            for section_id in orch.sections or ():
                add(section_id, composition_use(preset, "its prompt lists this section"))
            for section_id in orch.section_bodies:
                add(section_id, composition_use(preset, "it rewrites this section's body"))
            for target, values in (preset.settings or {}).items():
                add(target, composition_use(preset, _sets_via(values)))
            # A binding is the only statement here that neither end owns, and
            # it is also the only way a composition reaches a *subagent* -- so
            # it is worth naming the selector, not just the effect.
            for binding in preset.bindings or ():
                add(binding.plugin, composition_use(
                    preset, _BINDING_VIA.get(binding.effect, "a binding names it for {to}")
                    .format(to=binding.to)))

        # -- agent definitions: what each one *lists*, no resolve needed ----
        for name in sorted(defs):
            if name == ORCHESTRATOR_ID:
                continue
            comp = getattr(defs[name], "composition", None)
            if comp is None:
                continue
            servers = set()
            for pattern in comp.tools or ():
                for tool in tool_names:
                    if not _matches(pattern, tool):
                        continue
                    add(f"tool.{tool}", agent_use(name, (
                        f"matched by `{pattern}` in its tools" if _is_glob(pattern)
                        else "listed in its tools")))
                    server = _mcp_server(tool)
                    if server:
                        servers.add(server)
            for server in sorted(servers):
                add(f"mcp.{server}", agent_use(name, "it lists tools from this server"))
            for pattern in comp.spawns or ():
                for other in agent_ids:
                    if _matches(pattern, other):
                        add(f"agent.{other}", agent_use(name, "it may spawn it"))
            if comp.base:
                add(f"agent.{comp.base}", agent_use(name, "it derives from it (`base:`)"))
            for section_id in comp.sections or ():
                add(section_id, agent_use(name, "listed in its prompt sections"))
            for section_id in comp.section_bodies:
                add(section_id, agent_use(name, "it rewrites this section's body"))
            for target, values in (comp.settings or {}).items():
                add(target, agent_use(name, _sets_via(values)))

        return {
            target: sorted(uses.values(), key=lambda u: (u.kind != "composition", u.id))
            for target, uses in index.items()
        }

    def _agent_defs(self) -> dict[str, Any]:
        from quickcode.subagents.definitions import builtin_defs, load_defs

        if self.cwd is None:
            return builtin_defs()
        try:
            return load_defs(Path(self.cwd))
        except Exception as exc:  # the same rule as everywhere else here
            log.warning("agent definitions unreadable for used_by: %s", exc)
            return builtin_defs()

    # -- writing ---------------------------------------------------------

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        spec = self.get(plugin_id)
        if spec.required and not enabled:
            raise LockedSetting(f"{spec.title} is required and cannot be disabled")
        self._state.setdefault(plugin_id, {})["enabled"] = bool(enabled)
        self._persist(plugin_id, enabled=bool(enabled))

    def set_setting(self, plugin_id: str, key: str, value: Any,
                    *, confirmed: bool = False) -> Any:
        """Write one setting, enforcing its tier. Returns the coerced value."""
        spec = self.get(plugin_id)
        setting = spec.setting(key)
        if setting is None:
            raise UnknownSetting(f"{plugin_id} has no setting {key!r}")
        if setting.tier == "locked":
            raise LockedSetting(
                f"{setting.label()} is part of how QuickCode works and cannot "
                "be changed. You can still view it."
            )
        if setting.tier == "confirm" and not confirmed:
            raise NeedsConfirmation(plugin_id, key, setting.risk or setting.help)

        coerced = setting.coerce(value)
        slot = self._state.setdefault(plugin_id, {}).setdefault("settings", {})
        slot[key] = coerced
        self._persist(plugin_id, settings={key: coerced})
        return coerced

    def _persist(self, plugin_id: str, **fields: Any) -> None:
        if self.cwd is None:
            return  # ephemeral registry (tests, headless embedders)
        state_store.save_entry(self.cwd, plugin_id, **fields)

    # -- serialization for the UI ---------------------------------------

    def to_json(self, *, include_views: bool = False) -> dict[str, Any]:
        return {
            "plugins": [self.plugin_json(s.id, include_view=include_views)
                        for s in self.all()],
            "groups": [
                {"id": name, "plugins": [s.id for s in specs]}
                for name, specs in self.groups().items()
            ],
            "problems": [p.to_json() for p in self.problems],
        }

    def plugin_json(self, plugin_id: str, *, include_view: bool = False) -> dict[str, Any]:
        spec = self.get(plugin_id)
        values = self.settings(plugin_id)
        out: dict[str, Any] = {
            "id": spec.id,
            "kind": spec.kind,
            "title": spec.title,
            "description": spec.description,
            "group": spec.group or spec.kind,
            "source": spec.source,
            "required": spec.required,
            "enabled": self.is_enabled(spec.id),
            "tier": spec.tier(),
            "metadata": spec.metadata,
            # The six questions, in the order the UI asks them. Written once in
            # manifest.py and carried through verbatim -- a card that explained
            # itself differently from the registry would be describing an app
            # that does not exist.
            "summary": spec.summary,
            "affects": list(spec.affects),
            "audience": spec.audience,
            "consequence": spec.consequence,
            "locked_because": spec.locked_because,
            "recourse": _recourse_json(spec.recourse),
            "docs_anchor": spec.docs_anchor,
            # Authored plugins only: where the file is, and what it was copied
            # from. Both empty for anything that is code.
            "path": spec.path,
            "derived_from": spec.derived_from,
            # "If I change this, what moves." One index per registry build, so
            # this is a dict lookup however many plugins the caller renders.
            "used_by": [u.to_json() for u in self.used_by(spec.id)],
            "settings": [
                {
                    "key": s.key,
                    "type": s.type,
                    "title": s.label(),
                    "help": s.help,
                    "risk": s.risk,
                    "tier": s.tier,
                    "fact": s.fact,
                    "choices": list(s.choices),
                    "minimum": s.minimum,
                    "maximum": s.maximum,
                    "default": s.default,
                    "value": values.get(s.key, s.default),
                    "affects": list(s.affects),
                    "effect_detail": s.effect_detail,
                    "example": s.example,
                    # Most settings carry no reason of their own and inherit the
                    # plugin's, so these go through the fallback helpers rather
                    # than reading the raw fields.
                    "locked_because": spec.locked_because_for(s),
                    "recourse": _recourse_json(spec.recourse_for(s)),
                }
                for s in spec.settings
            ],
            "has_view": spec.view is not None,
        }
        if include_view:
            out["view"] = self.view_json(plugin_id)
        return out

    def view_json(self, plugin_id: str) -> dict[str, Any] | None:
        """Render a plugin's raw truth. Available whatever the tier."""
        spec = self.get(plugin_id)
        if spec.view is None:
            return None
        try:
            view = spec.view()
        except Exception as exc:  # a broken view must not break Settings
            log.warning("view for %s failed: %s", plugin_id, exc)
            return {"format": "text", "title": "unavailable",
                    "content": f"Could not render this plugin's definition: {exc}",
                    "path": ""}
        return {"format": view.format, "title": view.title,
                "content": view.content, "path": view.path}
