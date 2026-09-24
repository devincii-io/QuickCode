"""Putting a session together: the one path the app and ``quickcode -p`` share.

``ConversationManager.open()`` and the headless CLI each built the session's
own agent, and the copies drifted. ``-p`` ran the unfiltered default registry,
so a disabled plugin or a project's authored tools meant nothing there, and it
never resolved a composition, so neither the preset's tools, its spawn list nor
its model allow-lists applied. It rendered its prompt from other inputs, wrote
a session record without the composition a resume keeps, and handed its
subagents no pool, parent or definitions to be resolved against.

Every step both need is here once. What differs is what each caller wraps the
result in: the server a ``Conversation`` that fans events out to its windows,
the CLI a bare recorder around one turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quickcode.config import Config, Environment, Profile
from quickcode.core import profiles
from quickcode.core.agent import AgentInstance, Ledger, PermissionCallback
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.core.tasks import TaskBoard
from quickcode.hooks import session_hooks
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import Resolved, RuntimeLimits, narrower_mode
from quickcode.kernel.orchestrator import resolve_orchestrator
from quickcode.kernel.resolve import default_mode as settings_default_mode
from quickcode.kernel.resolve import runtime_limits
from quickcode.prompts.system import render_system_prompt
from quickcode.providers.base import ModelInfo, Provider
from quickcode.providers.choice import display_name
from quickcode.session.store import TASKS_DIRNAME, SessionStore
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.subagents.deps import SubagentDeps
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.bash_jobs import BashJobs
from quickcode.tools.registry import ToolRegistry

ROLES = ("worker", "orchestrator")


def task_board_path(cwd: Path, conv_id: str) -> Path:
    """Where a conversation's task board lives; ``purge_sessions`` removes it."""
    return Path(cwd) / TASKS_DIRNAME / conv_id / "board.json"


def resolve_role(profile: Profile, spec: str) -> str:
    """A model role ("worker", "orchestrator") to a slug; anything else passes
    through, because any id the provider accepts is allowed."""
    if spec in ROLES:
        return profile.resolve(spec)  # type: ignore[arg-type]
    return spec


def session_registry(pool: list[Any], resolved: Resolved) -> ToolRegistry:
    """The session's own agent's tools: the pool, cut to the resolved list.

    One registry for the session: the agent runs it, the permission engine
    reads its tools' declared shapes, and subagents select from it. Built from
    the resolved tool list, so the answer the UI gives and the tools the model
    is handed come from one computation.
    """
    return ToolRegistry([t for t in pool if t.name in resolved.tools])


def system_prompt(env: Environment, resolved: Resolved, *, model: str, provider: str,
                  plan: bool, headless: bool = False) -> str:
    """The orchestrator's prompt, rendered from the composition's frozen section
    bodies -- never from a fresh read of the settings files, so re-rendering it
    for a model switch cannot slip in prompt edits made since the session
    opened."""
    return render_system_prompt(
        env,
        model=model,
        provider=provider,
        headless=headless,
        plan=plan,
        orchestration=bool(resolved.spawns),
        overrides=dict(resolved.section_bodies),
    )


def frozen_composition(store: SessionStore, resuming: bool) -> Resolved | None:
    """A resumed session's recorded composition, if it has one.

    Sessions written before compositions existed (and ``-p`` sessions written
    before it recorded one) have no such record and fall back to re-resolving,
    including the fallback to ``standard`` when the preset is gone. A session
    that *does* carry one resumes from it and does not re-resolve: deleting a
    preset must not degrade a conversation already in flight.
    """
    if not resuming:
        return None
    return Resolved.from_json(store.meta().get("composition"))


@dataclass
class Session:
    """Everything one opened session is made of, before anyone drives it."""

    cwd: Path
    env: Environment
    profile: Profile
    store: SessionStore
    resuming: bool
    board: TaskBoard
    preset: Any
    # The session pool: everything this install has, minus the plugins that are
    # switched off. Distinct from any one agent's grant -- restricting the
    # orchestrator's tools does not restrict the session.
    pool: list[Any]
    # Snapshotted once: editing an agent definition reaches new sessions.
    defs: dict[str, AgentDef]
    resolved: Resolved
    limits: RuntimeLimits
    posture: profiles.PermissionProfile | None
    hooks: list
    agent: AgentInstance
    # Something asked for yolo that the app has not armed; the session started
    # lower. Each caller says so in its own voice.
    unarmed_yolo: bool = False

    @property
    def conv_id(self) -> str:
        return self.store.conv_id

    def wire(
        self,
        *,
        on_pane: Callable[..., None] | None,
        on_done: Callable[..., None] | None,
        on_bash_event: Callable[[dict[str, Any]], None] | None,
        adopt_task: Callable[[Any], None] | None = None,
    ) -> SubagentDeps:
        """Give the agent the tables it shares with its subagents: background
        shell jobs, and everything the ``agent`` tool needs to spawn one.

        A step of its own because the callbacks belong to whatever drives the
        session, and the server's ``Conversation`` needs the agent to exist
        before it does. ``adopt_task`` None means nothing outlives the turn to
        own a detached job, which is ``-p``.
        """
        ctx = self.agent.ctx
        permissions = self.agent.permissions
        bash_jobs = BashJobs(on_event=on_bash_event)
        ctx.extra["bash_jobs"] = bash_jobs
        deps = SubagentDeps(
            provider=self.agent.provider,
            profile=self.profile,
            env=self.env,
            mode_getter=lambda: permissions.mode,
            rules_getter=lambda: permissions.rules,
            cwd=self.cwd,
            depth=0,
            on_pane=on_pane,
            on_done=on_done,
            adopt_task=adopt_task,
            owner=self.agent,
            # The depth-0 carve-out. Children at depth 0 are intersected
            # against the session POOL, not the orchestrator's GRANT: the
            # orchestrator's restriction says what it does with its own hands,
            # not what the session may do. Passing the filtered registry here
            # is what made "delegate everything" hand every subagent an empty
            # toolset. Deeper levels keep intersecting against the parent's
            # grant, which is what ``deps.child()`` passes down.
            pool=self.pool,
            tool_pool=self.pool,
            parent=self.resolved,
            defs=self.defs,
            preset=self.preset,
            limits=self.limits,
            bash_jobs=bash_jobs,
            hooks=self.hooks,
        )
        ctx.extra["subagent"] = deps
        return deps

    def begin_log(self) -> None:
        """Queue a new session's opening record: model, preset, composition.

        Held, not written: opening a project opens a conversation, so writing
        here made merely starting the app leave an empty session on disk. The
        record still goes in front of whatever is said first. A resumed session
        already has one.
        """
        if self.resuming:
            return
        self.store.begin(
            title="", model=self.agent.model, cwd=str(self.cwd),
            preset=self.preset.id, composition=self.resolved.to_json(),
        )


def build_session(
    cwd: Path,
    config: Config,
    env: Environment,
    provider: Provider,
    *,
    pool: list[Any],
    conv_id: str | None = None,
    mode: str | None = None,
    model: str | None = None,
    provider_name: str | None = None,
    headless: bool = False,
    yolo_armed: bool = False,
    permission_cb: PermissionCallback | None = None,
    model_info: Callable[[str], ModelInfo | None] | None = None,
) -> Session:
    """Open a new session, or resume ``conv_id`` when its log exists.

    ``pool`` is the session pool (``kernel.resolve.session_pool``) over this
    caller's tools. ``mode`` is a starting mode the operator stated outright
    (``--mode``), which outranks the preset, the settings and the profile.
    ``model`` likewise outranks the last model picked and the profile's
    orchestrator role. Nothing here writes to disk; see ``Session.begin_log``.
    """
    cwd = Path(cwd)
    profile = config.profile
    store = SessionStore(cwd, conv_id)
    resuming = conv_id is not None and store.path.exists()
    board = TaskBoard.load(task_board_path(cwd, store.conv_id))
    ctx = ToolCtx(
        cwd=cwd,
        read_registry=ReadRegistry(),
        shell_name=env.shell_name,
        platform=env.platform,
        extra={"task_board": board},
    )

    # The preset is the session's plugin composition. A resumed session keeps
    # the one it started with: the conversation was already told which tools
    # it has, and changing them underneath it is a lie.
    preset = preset_module.resolve(cwd, store.meta().get("preset", "") if resuming else "")
    defs = load_defs(cwd)
    # Resolved off disk now; a resumed session runs on the limits recorded in
    # its own composition, so editing max_rounds or the compaction threshold
    # reaches the next session rather than one already in flight.
    resolved = frozen_composition(store, resuming) or resolve_orchestrator(
        pool=pool, preset=preset, defs=defs, cwd=cwd,
        resolve_model=lambda spec: resolve_role(profile, spec),
    )
    limits = runtime_limits(settings=resolved.settings)

    start, rules, posture, unarmed_yolo = _starting_posture(
        cwd, config, preset, resolved, explicit=mode, yolo_armed=yolo_armed,
    )
    registry = session_registry(pool, resolved)
    permissions = PermissionEngine(
        mode=start, rules=rules, root=cwd,
        yolo_accepted=yolo_armed,
        specs=registry.permission_specs(),
    )

    model = model or config.last_model or profile.resolve("orchestrator")
    info = model_info(model) if model_info is not None else None
    history = History(system_prompt(
        env, resolved, model=model,
        provider=provider_name or display_name(profile),
        plan=(start == Mode.plan), headless=headless,
    ))
    if resuming:
        history.messages = store.load_messages()

    # The user's command hooks ride alongside plan mode. Their settings are
    # read at the first turn rather than here, so trusting the project before
    # typing is enough for its hooks to apply (docs/HOOKS.md).
    hooks = session_hooks(cwd, session_id=store.conv_id,
                          transcript_path=str(store.path), resumed=resuming)
    agent = AgentInstance(
        name="main",
        provider=provider,
        registry=registry,
        history=history,
        ctx=ctx,
        permissions=permissions,
        model=model,
        permission_cb=permission_cb,
        context_length=info.context_length if info else None,
        hooks=hooks,
        limits=limits,
    )
    # The user's generation settings, applied as each session opens so a
    # change reaches the next one without a restart. The response budget in
    # particular is not a preference: the provider reserves credit against it,
    # and a balance too small for it is refused outright.
    agent.max_tokens = config.max_tokens
    agent.temperature = config.temperature
    if resuming:
        # Spend belongs to the session, not to this process.
        agent.ledger = Ledger.from_events(store.load_events())

    return Session(
        cwd=cwd, env=env, profile=profile, store=store, resuming=resuming,
        board=board, preset=preset, pool=pool, defs=defs, resolved=resolved,
        limits=limits, posture=posture, hooks=hooks, agent=agent,
        unarmed_yolo=unarmed_yolo,
    )


def _starting_posture(
    cwd: Path, config: Config, preset: Any, resolved: Resolved, *,
    explicit: str | None, yolo_armed: bool,
) -> tuple[Mode, Rules, profiles.PermissionProfile | None, bool]:
    """The mode the session starts in, the rules it runs, the profile they came
    from, and whether an unarmed yolo was refused on the way.

    The active profile's rules ride *on top of* the project's own (never
    instead of them, or picking a profile would revoke every "always allow"
    the user has accrued here), and its mode is where the session starts --
    above the preset's ``default_mode``, below an explicit ``--mode``: a profile
    is a file, and the flag is the operator saying it at launch.

    Applied identically whether or not this is a resume. No per-session mode
    is ever written to disk, so skipping the profile on resume would preserve
    nothing; it would swap the posture the user picked for the install default,
    and for every profile that narrows -- Read only starts in ``plan`` -- that
    is a resume coming back *wider* than the session it resumes.
    """
    mode_str = explicit or preset.default_mode or settings_default_mode(
        cwd, config.default_mode)
    try:
        mode = Mode(mode_str)
    except ValueError:
        mode = Mode.ask
    # Called through the module so a test can stand a profile in for the
    # files on disk, at one place for both callers.
    posture_mode, rules, posture = profiles.effective(cwd, Rules.load(cwd), fallback=mode)
    if not explicit:
        mode = posture_mode
    # Yolo needs the app to have armed it, whoever asks -- a profile, a
    # settings default or ``--mode``. ``set_mode`` and ``apply_posture`` hold
    # that line on a running session; opening one is the third door.
    unarmed_yolo = mode == Mode.yolo and not yolo_armed
    if unarmed_yolo:
        mode = Mode.ask
    # The starting mode may not begin above the ceiling. It stays live below
    # it -- rules decide this call, the ceiling decides what is ever possible,
    # and only the second is composition.
    return narrower_mode(mode, resolved.ceiling), rules, posture, unarmed_yolo
