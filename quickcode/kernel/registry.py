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
from pathlib import Path
from typing import Any

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
