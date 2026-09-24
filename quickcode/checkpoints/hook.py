"""Checkpoints as a ``LoopHook``: no tool knows it is being checkpointed.

``check_tool`` runs after the permission engine and before the call, with the
call id and the parsed arguments; ``tool_feedback`` runs after it, with the
result. Between them they bracket every call to a tool that declares it writes
files -- ``write``, ``edit``, an authored command tool with a ``path``
parameter, a plugin tool declaring ``path_target`` -- with nothing added to
any tool and nothing in the loop that names one.

Which calls are bracketed is read off ``PermissionSpec``, the same declaration
the permission gate reads: mutating, not a shell, and a path target (or
``permission_paths`` for a tool with several). A shell's command line names no
files anyone can check, which is why nothing ``bash`` does is recorded.

The "before" half is read when the gate has said yes or ask, not the instant
before the tool writes: in ``ask`` mode a permission prompt sits between them.
That gap is harmless for the tools that can report it: ``write`` and ``edit``
refuse a file that changed since it was read, and a tool that does not run a
program is taken at its word that an error means it wrote nothing. A program
(``executes``) may write and then fail, so its changes are recorded either way.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quickcode.checkpoints.events import FileCheckpointed
from quickcode.checkpoints.recorder import Capture, Checkpointer
from quickcode.core.hooks import LoopHook, ToolCheck
from quickcode.core.permissions import DEFAULT_SPEC, Decision
from quickcode.session.store import safe_conv_id

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance
    from quickcode.core.events import AssembledToolCall
    from quickcode.tools.base import Tool

log = logging.getLogger("quickcode.checkpoints")


def file_targets(tool: Tool, args: dict[str, Any], cwd: Path) -> list[Path]:
    """The files a call to ``tool`` says it will write, resolved against ``cwd``."""
    spec = getattr(tool, "permission", DEFAULT_SPEC)
    if not spec.mutates or spec.shell:
        return []
    raw: list[Any] = []
    several = getattr(tool, "permission_paths", None)
    if callable(several):
        try:
            raw = list(several(args))
        except Exception:  # noqa: BLE001 - a plugin's helper must not take the call down
            raw = []
    elif spec.path_target:
        declared = getattr(tool, "permission_target", None)
        raw = [(declared(args) if callable(declared) else "")
               or args.get(spec.target_field or "")]
    out: list[Path] = []
    for value in raw:
        if isinstance(value, str) and value.strip():
            path = Path(value)
            out.append(path if path.is_absolute() else Path(cwd) / path)
    return out


class CheckpointHook(LoopHook):
    id = "hook.checkpoints"

    def __init__(self, checkpointer: Checkpointer, *, subagent: bool = False) -> None:
        self.checkpointer = checkpointer
        # A subagent's turns are delegations inside the user's turn, so only
        # the session's own hook counts turns; the child's edits land in the
        # turn that is running when they happen.
        self.subagent = subagent
        self._pending: dict[str, list[Capture]] = {}

    @classmethod
    def for_session(cls, cwd: Path | None, session_id: str) -> CheckpointHook | None:
        if cwd is None or not safe_conv_id(session_id):
            return None
        return cls(Checkpointer(cwd, session_id))

    def for_subagent(self) -> CheckpointHook:
        return CheckpointHook(self.checkpointer, subagent=True)

    async def before_turn(self, agent: AgentInstance, text: str) -> str | None:
        if self.subagent:
            return None
        try:
            self.checkpointer.begin_turn()
            notice = await asyncio.to_thread(self.checkpointer.rewind_notice)
        except Exception as exc:  # noqa: BLE001 - checkpoints must never cost a turn
            log.warning("checkpoints: could not start turn: %s", exc)
            return None
        if notice:
            agent.queue_reminder(notice)
        return None

    async def check_tool(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, decision: Decision,
    ) -> ToolCheck | None:
        # One agent runs a mutating call alone, so whatever is still pending
        # belongs to a call that never reached its result (it raised).
        self._pending.clear()
        targets = file_targets(tool, args, agent.ctx.cwd)
        if not targets:
            return None
        try:
            captures = await asyncio.to_thread(self.checkpointer.capture, targets)
        except Exception as exc:  # noqa: BLE001
            log.warning("checkpoints: could not read %s's targets: %s", tool.name, exc)
            return None
        if captures:
            self._pending[call.id] = captures
        return None

    async def tool_feedback(
        self, agent: AgentInstance, call: AssembledToolCall, tool: Tool,
        args: dict, content: str, is_error: bool,
    ) -> str | None:
        captures = self._pending.pop(call.id, None)
        if not captures:
            return None
        if is_error and not getattr(tool, "permission", DEFAULT_SPEC).executes:
            return None
        try:
            turn, created = await asyncio.to_thread(
                self.checkpointer.commit, captures,
                call_id=call.id, tool=tool.name, agent=agent.name,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("checkpoints: could not record %s: %s", tool.name, exc)
            return None
        for entry in created:
            agent.bus.emit(FileCheckpointed(
                turn=turn, path=entry.path, call_id=call.id,
                tool=tool.name, created=entry.before is None,
                restorable=entry.restorable, reason=entry.reason,
            ))
        return None
