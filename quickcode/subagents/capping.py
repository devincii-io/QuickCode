"""A subagent's permission gate: never wider than the agent that spawned it.

Two things bound a child, and both are read off the spawner *live*:

* the mode -- ``min(spawner's current mode, the child's ceiling)``, re-read on
  every check. A snapshot taken at spawn let a background writer keep
  auto-edit after the user had cycled the parent down to plan.
* the restrictive rules -- the spawner's ``deny`` and ``ask`` lists. A child
  used to get an empty ``Rules()``, so every restriction the user wrote ended
  at the first delegation. ``allow`` is deliberately not inherited: it is the
  half that widens, and a child that cannot prompt has never been given it.

A child also cannot prompt anyone, so ``deny_prompt`` answers every ``ask``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from quickcode.core.agent import PermissionOutcome, PermissionRequest
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel.composition import cap_mode, narrower_mode


async def deny_prompt(_req: PermissionRequest) -> PermissionOutcome:
    return PermissionOutcome(
        allow=False,
        deny_message=(
            "A subagent cannot prompt the user for permission. This action needs "
            "a mode that allows it without asking, or the parent must do it."
        ),
    )


def restrictive(rules: Rules | None) -> Rules:
    """The part of ``rules`` a child inherits: everything that narrows."""
    if rules is None:
        return Rules()
    return Rules(ask=list(rules.ask), deny=list(rules.deny))


class ChildPermissions(PermissionEngine):
    """A ``PermissionEngine`` whose mode and rules are derived, not stored.

    ``mode`` and ``rules`` are the only two fields the engine reads its
    decisions from, so shadowing them with properties is the whole change;
    every check the engine makes stays the engine's own.
    """

    def __init__(
        self,
        root: Path,
        *,
        parent_mode: Callable[[], Mode],
        ceiling: Mode,
        parent_rules: Callable[[], Rules | None] | None = None,
    ) -> None:
        self.root = root
        self.yolo_accepted = False
        self.specs = {}
        self._parent_mode = parent_mode
        self._ceiling = ceiling
        self._parent_rules = parent_rules

    @property  # type: ignore[override]
    def mode(self) -> Mode:
        return cap_mode(self._parent_mode(), self._ceiling)

    @mode.setter
    def mode(self, value: Mode) -> None:
        # Setting a child's mode can only ever lower it: it narrows the
        # ceiling, and the spawner's live mode still caps whatever is left.
        self._ceiling = narrower_mode(self._ceiling, value)

    @property  # type: ignore[override]
    def rules(self) -> Rules:
        return restrictive(self._parent_rules() if self._parent_rules else None)

    @rules.setter
    def rules(self, _value: Rules) -> None:
        # Nothing a child is handed may replace what it inherits.
        return None
