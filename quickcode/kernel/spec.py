"""Plugin specification types.

Everything QuickCode can do is a plugin: the tools, the prompt sections, the
providers, the agents, the MCP servers, the loop hooks. The ones we ship are
"internal" plugins -- same shape as a third-party one, no privileged side
door. The list the Settings UI shows is the list the runtime uses; if those
two ever diverge the feature is a lie, so they read from one registry.

Mutability is declared per *setting*, not per plugin, in three tiers:

``free``     change it, nothing asks.
``confirm``  changeable, but it moves agent behaviour in ways that can break
             things, so the caller must pass ``confirmed=True``.
``locked``   not changeable, ever -- the tool-call protocol, the event log
             format, the report sanitizer.

``locked`` means "you cannot edit this". It never means "you cannot see it":
every plugin exposes a view of its raw truth at every tier.

Every plugin and every setting also answers the same six questions, in the same
order, wherever it is rendered. Consistency is the point: once a reader has
understood one card they can read all of them without looking twice.

===========  ==================  =====================================
Question     Field               Present when
===========  ==================  =====================================
WHAT         ``summary``         always
AFFECTS      ``affects``         always
WHO          ``audience``        always
IF CHANGED   ``consequence``     always -- neutral at every tier
WHY FIXED    ``locked_because``  locked only
INSTEAD      ``recourse``        locked only
===========  ==================  =====================================

``consequence`` and ``risk`` are different sentences and neither replaces the
other. ``consequence`` is neutral and says what becomes different; ``risk``
exists only on ``confirm`` settings and says what goes wrong. A ``free``
setting shows ``consequence`` alone, a ``confirm`` setting shows both, and a
``locked`` setting shows ``consequence`` plus the fixed-by-design pair.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Tier = Literal["free", "confirm", "locked"]

Kind = Literal[
    "tool",           # something the model can call
    "prompt_section", # a block of the system prompt
    "provider",       # a model backend
    "agent",          # a subagent definition
    "mcp_server",     # an external MCP process
    "policy",         # permission / sandbox rules
    "hook",           # loop lifecycle callbacks
    "panel",          # a UI surface
    "storage",        # session persistence
]

Source = Literal[
    "internal",   # shipped with QuickCode, declared in manifest.py
    "entrypoint", # third-party, discovered via importlib entry points
    "config",     # data-driven, e.g. an MCP server from settings.json
]

SettingType = Literal["string", "text", "bool", "int", "float", "enum", "list"]

ViewFormat = Literal["text", "markdown", "json", "schema"]

# Which surface a plugin or setting touches. Orthogonal to the provenance
# ``Layer`` vocabulary in ``kernel/problems.py``: ``affects`` says *what a
# thing changes*, ``layer`` says *which file said so*. Nothing merges them.
Effect = Literal[
    "prompt",       # the composed system prompt
    "tool_list",    # which tools an agent is offered
    "loop",         # how a turn runs: rounds, compaction, delegation
    "storage",      # what is written to disk and in what shape
    "ui",           # a surface in the web interface
    "permissions",  # whether a call is allowed, denied or prompted
    "models",       # which model answers
]

# Who the thing reaches. ``install`` is the honest answer for anything that is
# not per-agent at all -- a provider, an on-disk format.
Audience = Literal["orchestrator", "named_agents", "all_agents", "install"]


@dataclass(frozen=True)
class Recourse:
    """What a user *can* do when the thing in front of them is fixed.

    A locked setting must never be a dead end. This is the next action, and it
    is real: duplicate the plugin to get an editable copy, change the knob that
    actually governs this one, or read the contract in full.
    """

    action: Literal["duplicate", "author", "settings", "docs", "none"]
    label: str          # imperative, e.g. "Duplicate this agent to edit it"
    target: str = ""    # plugin id to duplicate, kind to seed New..., or a doc path


@dataclass(frozen=True)
class SettingSpec:
    """One knob on a plugin, with the tier that governs changing it."""

    key: str
    type: SettingType
    default: Any
    tier: Tier
    title: str = ""
    help: str = ""
    # ``confirm`` only: what exactly goes wrong if this is changed carelessly.
    # The dialog names this instead of asking "are you sure?" about nothing.
    risk: str = ""
    # enum only: the admissible values, in display order.
    choices: tuple[str, ...] = ()
    # int/float only.
    minimum: float | None = None
    maximum: float | None = None

    # -- the explanation layer, narrower than the plugin's ------------------
    # Which surfaces this one knob moves. ``max_rounds`` affects ``loop``
    # only, even though its plugin also touches the tool list.
    affects: tuple[Effect, ...] = ()
    # The mechanical sentence: what the value literally does, in terms a reader
    # can check against the behaviour they see.
    effect_detail: str = ""
    # ``locked`` only: the engineering reason it is not a knob. Falls back to
    # the plugin's when empty.
    locked_because: str = ""
    # Required whenever ``locked_because`` is set. Falls back to the plugin's.
    recourse: Recourse | None = None
    # A good non-default value, used as the input placeholder. Never the
    # default -- the default is already shown.
    example: str = ""
    # This row states something the object declares about *itself* rather than
    # a knob that was taken away. A tool's ``read_only`` is the case that
    # matters: the tool's class decides it, the runtime reads it on every call,
    # and no invariant is being defended by refusing to edit it -- there is
    # simply nothing there to edit. Facts are still ``locked`` (writing one
    # must fail) but they do not make their plugin read as locked; see
    # ``PluginSpec.tier``.
    fact: bool = False

    def label(self) -> str:
        return self.title or self.key.replace("_", " ").capitalize()

    def coerce(self, value: Any) -> Any:
        """Best-effort cast of a JSON value to this setting's type.

        The UI posts strings for everything; refusing a "12" for an int knob
        would be pedantry. A value that cannot be cast raises ValueError and
        the caller reports it against this key.
        """
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "int":
            return self._clamp(int(value))
        if self.type == "float":
            return self._clamp(float(value))
        if self.type == "enum":
            text = str(value)
            if self.choices and text not in self.choices:
                raise ValueError(f"{text!r} is not one of {', '.join(self.choices)}")
            return text
        if self.type == "list":
            if isinstance(value, str):
                return [part.strip() for part in value.splitlines() if part.strip()]
            return list(value)
        return str(value)

    def _clamp(self, value: float) -> float | int:
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{value} is below the minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{value} is above the maximum {self.maximum}")
        return value


@dataclass(frozen=True)
class PluginView:
    """The raw truth behind a plugin, for the "show me what this actually is"
    affordance in the UI. Available even when every setting is locked."""

    format: ViewFormat
    content: str
    title: str = ""
    # Where this lives on disk, when it does -- so the UI can offer "open file".
    path: str = ""


@dataclass
class PluginSpec:
    """What a plugin is, independent of whether it is currently switched on."""

    id: str            # stable slug: "tool.bash", "prompt.tone", "agent.explore"
    kind: Kind
    title: str
    description: str = ""
    # Card grouping in Settings -> Plugin configuration, e.g. "Shell", "Prompt".
    group: str = ""
    source: Source = "internal"
    # Internal plugins that hold the app together: switchable off would mean a
    # broken agent, so the UI shows them as permanent rather than offering a
    # toggle that fails.
    required: bool = False
    enabled_by_default: bool = True
    settings: tuple[SettingSpec, ...] = ()
    # Declared tier for the plugin as a whole. Set it when the plugin has no
    # editable settings to infer from -- a locked thing with no knobs must not
    # read as freely editable just because there is nothing to edit.
    tier_hint: Tier | None = None
    # Free-form facts for the UI (a tool's schema, an agent's model policy).
    metadata: dict[str, Any] = field(default_factory=dict)
    # Lazily rendered so building the registry never reads a file or a prompt.
    view: Callable[[], PluginView] | None = None

    # -- the explanation layer ---------------------------------------------
    # WHAT. One sentence, <= 90 characters, plain language. Not a restatement
    # of the title and not the id; ``description`` stays as the longer line.
    summary: str = ""
    # AFFECTS. Every surface this touches. Empty is a bug, not a default.
    affects: tuple[Effect, ...] = ()
    # WHO. Truthful about scope: "all_agents" is wrong for something only the
    # orchestrator ever sees.
    audience: Audience = "install"
    # IF CHANGED. What becomes different when this is disabled or reconfigured.
    # Neutral at every tier -- this is not ``risk``.
    consequence: str = ""
    # WHY FIXED. Required when ``tier()`` is ``locked``. Names the invariant,
    # not the policy: "the trajectory replays by sequence number, so the record
    # shape is fixed".
    locked_because: str = ""
    # INSTEAD. Required whenever ``locked_because`` is set.
    recourse: Recourse | None = None
    # The long form, e.g. "docs/PERMISSIONS.md#modes".
    docs_anchor: str = ""

    def setting(self, key: str) -> SettingSpec | None:
        for spec in self.settings:
            if spec.key == key:
                return spec
        return None

    def defaults(self) -> dict[str, Any]:
        return {s.key: s.default for s in self.settings}

    def locked_because_for(self, setting: SettingSpec) -> str:
        """A setting's fixed-by-design reason, falling back to the plugin's.

        Most locked settings are locked for the same reason their plugin is,
        and repeating the sentence per knob is how prose starts drifting from
        behaviour. A setting states its own reason only when it differs.
        """
        return setting.locked_because or self.locked_because

    def recourse_for(self, setting: SettingSpec) -> Recourse | None:
        """The way forward for a setting, falling back to the plugin's."""
        return setting.recourse or self.recourse

    def tier(self) -> Tier:
        """The strictest tier among this plugin's *knobs*.

        Used for the badge on a collapsed card: a plugin holding one locked
        knob should not read as freely editable at a glance.

        Settings marked ``fact`` are skipped, and that exclusion is the whole
        point of the flag. Every tool's only setting is its declared
        ``read_only``, so the strictest-tier rule badged all thirteen tool
        cards ``locked`` -- which told a reader nothing (they were all the
        same) and was untrue besides: a tool is not fixed by design, it can be
        switched off like anything else, and its read-only-ness is a fact about
        the class rather than a knob somebody removed. A plugin whose settings
        are all facts has no knobs at all, so nothing about it needs
        confirming: it badges ``free``, which is exactly what its one real
        affordance -- the enable toggle -- is.

        The setting keeps its own ``locked`` tier and still refuses writes;
        this is only about what the card claims about itself.
        """
        if self.tier_hint is not None:
            return self.tier_hint
        order: dict[Tier, int] = {"free": 0, "confirm": 1, "locked": 2}
        worst: Tier = "free"
        for s in self.settings:
            if s.fact:
                continue
            if order[s.tier] > order[worst]:
                worst = s.tier
        return worst


class PluginError(Exception):
    """Base for refusals the UI should render as a message, not a stack trace."""


class LockedSetting(PluginError):
    """Raised when something tries to write a ``locked`` setting."""


class NeedsConfirmation(PluginError):
    """Raised when a ``confirm`` setting is written without ``confirmed=True``.

    Carries the human-readable reason so the dialog can name the risk instead
    of asking "are you sure?" about nothing in particular.
    """

    def __init__(self, plugin_id: str, key: str, reason: str = "") -> None:
        self.plugin_id = plugin_id
        self.key = key
        self.reason = reason
        super().__init__(reason or f"{plugin_id}.{key} needs confirmation")


class UnknownPlugin(PluginError):
    pass


class UnknownSetting(PluginError):
    pass
