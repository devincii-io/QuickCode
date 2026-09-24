"""Moved to :mod:`quickcode.session.wire`, which the session recorder owns.

Kept so plugins importing ``register_event`` from here keep working; these are
the same objects, so a registration through either name lands in one table.
"""

from quickcode.session.wire import (
    LOG_RESULT_CAP,
    LOGGED_TYPES,
    event_to_json,
    loggable,
    plugin_logged,
    register_event,
)

__all__ = [
    "LOGGED_TYPES",
    "LOG_RESULT_CAP",
    "event_to_json",
    "loggable",
    "plugin_logged",
    "register_event",
]
