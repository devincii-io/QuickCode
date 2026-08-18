"""The plugin kernel: what QuickCode consists of, and what may be changed.

``spec``      the types (plugins, settings, the free/confirm/locked tiers)
``state``     persisted enable flags and setting values
``registry``  the live registry, with tier enforcement on writes
``manifest``  the internal plugins we ship, derived from the live objects
``bootstrap`` assembles a registry for one project
"""

from quickcode.kernel.bootstrap import build_registry
from quickcode.kernel.registry import PluginRegistry
from quickcode.kernel.spec import (
    LockedSetting,
    NeedsConfirmation,
    PluginSpec,
    PluginView,
    SettingSpec,
    UnknownPlugin,
    UnknownSetting,
)

__all__ = [
    "LockedSetting",
    "NeedsConfirmation",
    "PluginRegistry",
    "PluginSpec",
    "PluginView",
    "SettingSpec",
    "UnknownPlugin",
    "UnknownSetting",
    "build_registry",
]
