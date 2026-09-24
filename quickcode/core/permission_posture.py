"""The permission engine a session would run, built without opening one.

A dry run of the gate (``core/permission_explain.py``) is only worth having if
it asks the engine a real session asks. So this assembles that engine from the
same pieces ``ConversationManager.open()`` assembles it from, in the same
order: the starting mode (``--mode``, then the composition's
``default_mode``, then the ``runtime.permissions`` setting), the project's
rules from ``Rules.load``, the active profile merged over them by
``profiles.effective``, the composition's ceiling, and each tool's declared
``PermissionSpec``. None of those is recomputed here -- each step is the
function ``open()`` calls. ``tests/test_permissions_api.py`` opens a real
session beside this and checks the two agree.

A live conversation needs none of this: its engine already exists, with the
"always allow" answers it accrued, and ``for_conversation`` wraps it --
``for_review`` narrows that to the agent whose prompt is on screen.

Two what-ifs sit on top, for the pages that teach and preview rules: leaving
out the project's rules or its active profile, and adding rules that are
written nowhere yet (a profile draft, a sandbox). The added rules are merged
by ``PermissionProfile.merged`` -- the way a profile's are -- so they can only
join the lists the engine already walks, never reorder them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from quickcode.core.permissions import Mode, PermissionEngine, Rules


@dataclass(frozen=True)
class Posture:
    """An engine to ask, and what it was built from -- for the explanation."""

    engine: PermissionEngine
    # Every tool the session could reach, by name: the gate needs the tool
    # object (``permission_target``, ``permission_paths``), not only its spec.
    tools: dict[str, Any]
    # The names the orchestrator is actually handed. A tool outside this set is
    # never called in the first place, whatever the gate would say.
    offered: frozenset[str]
    # Where the mode came from: "request", "session", or "new session".
    mode_source: str
    profile_id: str = ""
    conv_id: str = ""
    yolo_armed: bool = False
    # Where a live session's shell stands after its last `cd` -- the loop hands
    # the gate this for every shell call, so a question about one has to too.
    shell_cwd: Path | None = None
    # Whether the project's own settings rules are in the engine.
    project_rules: bool = True
    # Rules added for this question only, as a ``PermissionProfile``.
    extra: Any = None

    def _engine(self, **changes: Any) -> PermissionEngine:
        """A copy of the engine. Never the engine itself: a live session's
        engine is the live gate, and a question must not change its answers."""
        e = self.engine
        fields = {"mode": e.mode, "rules": e.rules, "root": e.root,
                  "yolo_accepted": e.yolo_accepted, "specs": e.specs, **changes}
        return PermissionEngine(**fields)

    def with_mode(self, mode: Mode | None) -> Posture:
        """The same posture asked about under another mode."""
        if mode is None:
            return self
        return replace(self, engine=self._engine(mode=mode), mode_source="request")

    def with_rules(self, extra: Any) -> Posture:
        """The same posture with ``extra`` (a ``PermissionProfile``) merged in."""
        if extra is None:
            return self
        return replace(self, engine=self._engine(rules=extra.merged(self.engine.rules)),
                       extra=extra)


def for_new_session(
    cwd: Path,
    *,
    config: Any,
    tools: Iterable[Any],
    default_mode: str | None = None,
    allow_yolo: bool = False,
    resolve_model: Callable[[str], str] | None = None,
    project_rules: bool = True,
    use_profile: bool = True,
) -> Posture:
    """The engine a session opened in ``cwd`` right now would start with.

    ``default_mode`` is ``--mode`` (the manager's ``default_mode``), and
    ``tools`` is the install's tool registry before the session pool is taken.
    ``project_rules`` and ``use_profile`` leave those layers out.
    """
    from quickcode.core.profiles import effective as effective_posture
    from quickcode.kernel import preset as preset_module
    from quickcode.kernel.composition import ORCHESTRATOR_ID, narrower_mode
    from quickcode.kernel.resolve import default_mode as resolved_default_mode
    from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
    from quickcode.subagents.definitions import load_defs
    from quickcode.tools.registry import ToolRegistry

    pool = session_pool(cwd, list(tools))
    preset = preset_module.resolve(cwd)
    resolved = resolve_composition(
        ORCHESTRATOR_ID, pool=pool, preset=preset, defs=load_defs(cwd), cwd=cwd,
        max_depth=runtime_limits(cwd).max_depth, resolve_model=resolve_model,
    )
    mode_str = (default_mode or preset.default_mode
                or resolved_default_mode(cwd, config.default_mode))
    try:
        mode = Mode(mode_str)
    except ValueError:
        mode = Mode.ask
    base = Rules.load(cwd) if project_rules else Rules()
    profile = None
    rules = base
    if use_profile:
        posture_mode, rules, profile = effective_posture(cwd, base, fallback=mode)
        if not default_mode:
            mode = posture_mode
    # Unarmed, a yolo from any source opens the session in ask
    # (``session/assemble._starting_posture``), so that is what it is asked in.
    if mode == Mode.yolo and not allow_yolo:
        mode = Mode.ask
    mode = narrower_mode(mode, resolved.ceiling)
    registry = ToolRegistry([t for t in pool if t.name in resolved.tools])
    engine = PermissionEngine(mode=mode, rules=rules, root=cwd,
                              yolo_accepted=allow_yolo, specs=registry.permission_specs())
    return Posture(
        engine=engine,
        tools={t.name: t for t in pool},
        offered=frozenset(resolved.tools),
        mode_source="new session",
        profile_id=profile.id if profile else "",
        yolo_armed=allow_yolo,
        project_rules=project_rules,
    )


def for_conversation(conv: Any, *, pool: Iterable[Any] = (),
                     yolo_armed: bool = False) -> Posture:
    """The live gate of an open conversation: its mode, and its rules including
    what the user approved during it. ``pool`` adds the tools the session could
    reach but was not handed, so asking about one says so rather than 404."""
    agent = conv.agent
    tools = {t.name: t for t in pool}
    tools.update(agent.registry.tools)
    return Posture(
        engine=agent.permissions,
        tools=tools,
        offered=frozenset(agent.registry.tools),
        mode_source="session",
        profile_id=getattr(conv, "profile_id", "") or "",
        conv_id=conv.conv_id,
        yolo_armed=yolo_armed,
        shell_cwd=agent.ctx.extra.get("bash_cwd") if agent.ctx else None,
    )


def for_review(conv: Any, gated: Any, *, pool: Iterable[Any] = (),
               yolo_armed: bool = False) -> Posture:
    """The gate that raised a pending prompt, as it stood: the engine of the
    agent that asked -- a subagent's is capped, and carries the session's deny
    and ask rules but none of its allows -- and where that agent's shell stood.
    ``gated`` is the request's ``GatedCall``; "Why?" on a prompt asks this."""
    base = for_conversation(conv, pool=pool, yolo_armed=yolo_armed)
    name = gated.tool.name
    return replace(base, engine=gated.engine, shell_cwd=gated.cwd,
                   tools={**base.tools, name: gated.tool}, offered=base.offered | {name})


def what_if_rules(raw: Any) -> Any:
    """Rules written nowhere yet, as a ``PermissionProfile`` -- validated by the
    profile loader, so a line the engine could never match is reported rather
    than silently matching nothing. ``None`` for no rules. Raises ``ValueError``
    for a shape that is not ``{"allow"|"ask"|"deny": [str, ...]}``."""
    from quickcode.core.profiles import PermissionProfile

    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"allow", "ask", "deny"}:
        raise ValueError("'rules' must be an object with allow, ask and deny lists")
    for kind, rules in raw.items():
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise ValueError(f"'rules.{kind}' must be a list of strings")
    if not any(raw.values()):
        return None
    return PermissionProfile.from_dict("what-if", raw, layer="user")
