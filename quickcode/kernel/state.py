"""Persisted plugin state: what is switched on, and what its knobs are set to.

Layered the same way permissions and MCP servers already are
(``core/permissions.py``, ``plugins/mcp.py``): the user's
``~/.quickcode/settings.json`` underneath, the project's
``.quickcode/settings.json`` on top. Writes always land in the project file --
a plugin tuned for one repo has no business changing another.

Shape, alongside the existing ``permissions`` and ``mcpServers`` keys::

    {
      "plugins": {
        "tool.bash": {"enabled": true, "settings": {"timeout_s": 120}},
        "prompt.tone": {"settings": {"body": "..."}}
      }
    }

Unknown plugin ids are preserved on write rather than pruned: a settings file
shared with a machine that has an extra plugin installed must survive a round
trip through this one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from quickcode.config import CONFIG_DIR
from quickcode.kernel.problems import Problem, Provenance

log = logging.getLogger("quickcode.kernel.state")

SETTINGS_DIRNAME = ".quickcode"
SETTINGS_FILENAME = "settings.json"
LOCAL_SETTINGS_FILENAME = "settings.local.json"
PLUGINS_KEY = "plugins"
PRESETS_KEY = "presets"


def user_settings_path() -> Path:
    return CONFIG_DIR / SETTINGS_FILENAME


def project_settings_path(cwd: Path) -> Path:
    return Path(cwd) / SETTINGS_DIRNAME / SETTINGS_FILENAME


def local_settings_path(cwd: Path) -> Path:
    """The gitignored sibling. Permissions and MCP read it; plugin and preset
    state deliberately do not -- see ``local_settings_problems``."""
    return Path(cwd) / SETTINGS_DIRNAME / LOCAL_SETTINGS_FILENAME


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # A hand-edited settings file with a stray comma must not take the app
        # down; the layer is skipped and the user is told once, in the log.
        log.warning("ignoring unreadable settings at %s: %s", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _entries(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    section = raw.get(PLUGINS_KEY)
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if isinstance(v, dict)}


def _project_entries(cwd: Path, trusted: bool | None) -> dict[str, dict[str, Any]]:
    """The project layer's plugin state, minus what it may not set on its own.

    A project committing ``runtime.permissions.default_mode: "yolo"`` is asking
    for the session to open in bypass mode, which is the same grant a committed
    MCP server asks for and goes through the same gate. What survives the gate
    is ``trust.project_may_state``'s answer, so this file and the trust report
    refuse exactly the same values; what is dropped falls back to the user's
    own setting, which is a safe answer and a working one.
    """
    from quickcode.security import trust

    entries = _entries(_read(project_settings_path(cwd)))
    if trust.resolve_trust(cwd, trusted):
        return entries

    out: dict[str, dict[str, Any]] = {}
    refused: list[str] = []
    for plugin_id, entry in entries.items():
        if plugin_id not in trust.GATED_PLUGIN_IDS:
            out[plugin_id] = entry
            continue
        settings = entry.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        kept = {k: v for k, v in settings.items() if trust.project_may_state(k, v)}
        refused += [f"{plugin_id}.{k}" for k in settings if k not in kept]
        # Everything that is not a setting stays: ``enabled`` can only switch a
        # plugin off, which narrows, and narrowing was never what needed a
        # grant.
        rest = {k: v for k, v in entry.items() if k != "settings"}
        if kept:
            rest["settings"] = kept
        if rest:
            out[plugin_id] = rest
    if refused:
        log.warning("project %s is not trusted; ignoring %s",
                    cwd, ", ".join(sorted(refused)))
    return out


def load_state(cwd: Path | None, *, trusted: bool | None = None) -> dict[str, dict[str, Any]]:
    """Merged plugin state, project overriding user, per plugin id.

    The merge is per *field*, not per plugin: a project that pins one knob
    should not silently discard the user's setting for the others.
    """
    merged: dict[str, dict[str, Any]] = {}
    layers = [_entries(_read(user_settings_path()))]
    if cwd is not None:
        layers.append(_project_entries(cwd, trusted))

    for layer in layers:
        for plugin_id, entry in layer.items():
            slot = merged.setdefault(plugin_id, {})
            if "enabled" in entry:
                slot["enabled"] = bool(entry["enabled"])
            settings = entry.get("settings")
            if isinstance(settings, dict):
                slot.setdefault("settings", {}).update(settings)
    return merged


def layer_states(
    cwd: Path | None, *, trusted: bool | None = None,
) -> list[tuple[str, Path, dict[str, dict[str, Any]]]]:
    """The plugin state of each configuration layer, kept separate.

    ``load_state`` merges them, which is what a consumer asking "what is this
    knob set to" wants. The resolver wants the opposite: it has to name the
    layer that set a value, so it needs them apart.
    """
    out: list[tuple[str, Path, dict[str, dict[str, Any]]]] = []
    user = user_settings_path()
    out.append(("user", user, _entries(_read(user))))
    if cwd is not None:
        project = project_settings_path(cwd)
        out.append(("project", project, _project_entries(cwd, trusted)))
    return out


def disabled_plugin_ids(cwd: Path | None) -> set[str]:
    """Plugins switched off, at any layer.

    This is the whole authoring surface of the session-wide revoke: a tool
    whose plugin is disabled leaves the pool entirely, for every agent at every
    depth. Restricting one agent's grant is a different statement and is made
    somewhere else.
    """
    return {
        plugin_id for plugin_id, entry in load_state(cwd).items()
        if entry.get("enabled") is False
    }


def plugin_setting(cwd: Path | None, plugin_id: str, key: str, default: Any = None,
                   *, trusted: bool | None = None) -> Any:
    """One persisted plugin setting, without building a registry.

    Deliberately no spec coercion: the caller knows the type it wants and the
    registry is the place that validates. Used by the session path, which must
    not pay for a full plugin inventory to read one knob.
    """
    entry = load_state(cwd, trusted=trusted).get(plugin_id) or {}
    settings = entry.get("settings")
    if isinstance(settings, dict) and key in settings:
        return settings[key]
    return default


def local_settings_problems(cwd: Path | None) -> list[Problem]:
    """``settings.local.json`` is not a plugin or preset layer, and says so.

    The local file is for accreted "always allow" rules, not configuration:
    permissions and MCP read it because those are decisions a session makes
    about itself, while plugin and preset state is something a person writes
    down. Leaving the asymmetry is the decision; leaving it *silent* was the
    defect, because a plugin setting placed there is ignored without a word.
    """
    if cwd is None:
        return []
    path = local_settings_path(cwd)
    if not path.exists():
        return []
    raw = _read(path)
    found = [key for key in (PLUGINS_KEY, PRESETS_KEY) if isinstance(raw.get(key), dict)]
    if not found:
        return []
    return [
        Problem(
            code="local_settings_ignored",
            severity="info",
            message=(
                f"{path.name} contains {' and '.join(found)}, which is not read "
                "from that file. Only permissions and mcpServers are."
            ),
            fix=f"Move those keys into {SETTINGS_FILENAME}.",
            subject=str(path),
            field=found[0],
            provenance=Provenance(layer="project", source=path.name, path=str(path)),
        )
    ]


def untrusted_project_problems(
    cwd: Path | None, *, trusted: bool | None = None,
) -> list[Problem]:
    """What this project asked for that its trust status does not cover.

    The gate's fallback is the user's own configuration, which is a working
    session -- but a session that quietly runs on different rules than the file
    in front of you states is the same defect ``local_settings_problems``
    exists to fix, one layer up. So the refusal is named, with the keys as they
    are written and the one action that changes the answer.
    """
    from quickcode.security import trust

    if cwd is None or trust.resolve_trust(cwd, trusted):
        return []
    keys = sorted(trust.project_policy_config(cwd))
    if not keys:
        return []
    return [
        Problem(
            code="project_settings_ignored",
            severity="warning",
            message=(
                f"this project sets {len(keys)} permission "
                f"{'settings' if len(keys) != 1 else 'setting'} "
                f"({', '.join(keys)}) and is not trusted, so "
                f"{'they are' if len(keys) != 1 else 'it is'} ignored and your "
                "own settings apply instead"
            ),
            fix=("Read them, then trust this project to let them apply. A "
                 "committed allowlist or default mode widens what the agent "
                 "may do without asking, which is the same decision trusting "
                 "an MCP server is."),
            subject="project", field="trust",
            provenance=Provenance(layer="project", source=SETTINGS_FILENAME,
                                  path=str(project_settings_path(cwd))),
        )
    ]


def prompt_overrides(cwd: Path | None) -> dict[str, str]:
    """Section id -> replacement body, for the system prompt.

    Read straight from state rather than through the registry: composing the
    prompt happens on every session open, and it should not have to build a
    full plugin inventory to find out whether anything was customised.
    """
    out: dict[str, str] = {}
    for plugin_id, entry in load_state(cwd).items():
        if not plugin_id.startswith("prompt."):
            continue
        body = (entry.get("settings") or {}).get("body")
        if isinstance(body, str) and body.strip():
            out[plugin_id] = body
    return out


def save_entry(cwd: Path, plugin_id: str, *, enabled: bool | None = None,
               settings: dict[str, Any] | None = None) -> None:
    """Merge one plugin's state into the project settings file.

    Everything else in the file -- permissions, mcpServers, other plugins --
    is read, updated in place and written back, so this never clobbers config
    it does not own.
    """
    path = project_settings_path(cwd)
    raw = _read(path)
    section = raw.get(PLUGINS_KEY)
    if not isinstance(section, dict):
        section = {}
    entry = section.get(plugin_id)
    if not isinstance(entry, dict):
        entry = {}

    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if settings:
        current = entry.get("settings")
        if not isinstance(current, dict):
            current = {}
        current.update(settings)
        entry["settings"] = current

    section[plugin_id] = entry
    raw[PLUGINS_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    # The project settings file is covered by the trust hash, so saving a
    # setting from the Settings page untrusted the project it was saved in --
    # silently switching off its allow rules and its MCP servers. A change the
    # user made here is not a reason to stop trusting the project.
    from quickcode.security.trust import keep_trust

    with keep_trust(cwd):
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
