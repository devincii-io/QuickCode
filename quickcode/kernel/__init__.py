"""The plugin kernel: what QuickCode consists of, and what may be changed.

``spec``      the types (plugins, settings, the free/confirm/locked tiers)
``state``     persisted enable flags and setting values
``registry``  the live registry, with tier enforcement on writes
``manifest``  the internal plugins we ship, derived from the live objects
``bootstrap`` assembles a registry for one project

The names below are loaded on first use (PEP 562). Importing any submodule
imports this package first, and ``bootstrap`` pulls in every manifest module
and all of its prose: eager, it made ``quickcode.kernel.resolve`` -- which the
runtime imports to open every session -- pay for the Settings page too.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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

_LAZY = {
    "build_registry": "quickcode.kernel.bootstrap",
    "PluginRegistry": "quickcode.kernel.registry",
    "LockedSetting": "quickcode.kernel.spec",
    "NeedsConfirmation": "quickcode.kernel.spec",
    "PluginSpec": "quickcode.kernel.spec",
    "PluginView": "quickcode.kernel.spec",
    "SettingSpec": "quickcode.kernel.spec",
    "UnknownPlugin": "quickcode.kernel.spec",
    "UnknownSetting": "quickcode.kernel.spec",
}

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


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        # Also how ``from quickcode.kernel import preset`` reaches the import
        # system's submodule fallback.
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
