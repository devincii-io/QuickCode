"""The session's own agent, resolved the one way every caller needs it.

Opening a session, switching its composition, the workbench's inventory and
its per-agent view all resolve ``@orchestrator`` with the same shape of call:
no parent, spawner depth 0, and the depth limit the settings files state. Each
of them spelled that call out by hand, and a site that forgot ``max_depth``
would silently resolve against the dataclass default rather than the user's
setting.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from quickcode.kernel.composition import ORCHESTRATOR_ID, Resolved
from quickcode.kernel.resolve import resolve_composition, runtime_limits


def resolve_orchestrator(
    *,
    pool: list[Any],
    preset: Any,
    defs: dict[str, Any],
    cwd: Path | None,
    max_depth: int | None = None,
    resolve_model: Callable[[str], str] | None = None,
) -> Resolved:
    """``@orchestrator`` under ``preset``: nobody's child, at depth 0.

    ``max_depth`` defaults to what ``cwd``'s settings say now, which is what a
    session being opened and a live view both want. A caller resolving against
    a snapshot, or already holding the limits, passes its own. Never raises.
    """
    if max_depth is None:
        max_depth = runtime_limits(cwd).max_depth
    return resolve_composition(
        ORCHESTRATOR_ID,
        pool=pool,
        preset=preset,
        defs=defs,
        cwd=cwd,
        parent=None,
        depth=0,
        max_depth=max_depth,
        resolve_model=resolve_model,
    )
