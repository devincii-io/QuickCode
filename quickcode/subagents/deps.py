"""The state one level of the agent tree hands the ``agent`` tool.

``SubagentDeps`` rides in ``ToolCtx.extra['subagent']``. Most of it is shared
down the whole tree -- the counter, the roster, the job registry, the spend
per id -- so a conversation can cap, find, cancel and account for every child
wherever it was spawned. A few fields are per level: whose deps these are, the
live mode and rules children are capped by, and the composition they are
narrowed against.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quickcode.config import Environment, Profile
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel.composition import Resolved, RuntimeLimits
from quickcode.providers.base import Provider
from quickcode.subagents import worktree
from quickcode.subagents.definitions import AgentDef, load_defs
from quickcode.subagents.jobs import JobRecord
from quickcode.tools.registry import core_tools

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance, EventBus


@dataclass
class SubagentDeps:
    """Everything the ``agent`` tool needs to build a child, carried in
    ``ToolCtx.extra['subagent']``.

    ``mode_getter`` and ``rules_getter`` are read live, by every check a child
    makes, not only at spawn: the user cycling Shift+Tab caps children that
    are already running as well as later delegations (see ``capping.py``).
    """

    provider: Provider
    profile: Profile
    env: Environment
    mode_getter: Callable[[], Mode]
    cwd: Path
    depth: int = 0
    # The spawner's rules. Its ``deny`` and ``ask`` lists bind every child;
    # None means there are none to inherit (a bare embedder).
    rules_getter: Callable[[], Rules | None] | None = None
    # Shared across the whole conversation's agent tree.
    counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    spawned: list[str] = field(default_factory=list)
    # Every spawned child, keyed by agent_id, so ``send_message`` can resume one
    # by id from anywhere in the tree. Shared down the tree like counter/spawned.
    roster: dict[str, AgentInstance] = field(default_factory=dict)
    # Detached runs, keyed by the same agent_id. Shared down the tree for the
    # same reason the roster is: a job started three levels down is still this
    # conversation's job to cap, cancel and clean up.
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    # UI hook: called synchronously at spawn with (agent_id, definition name,
    # the child's EventBus) so a live pane can subscribe to the child's stream.
    # Optional — headless runs leave it None.
    on_pane: Callable[[str, str, EventBus], None] | None = None
    # The other end of ``on_pane``: called once, when a child reaches a terminal
    # state, with (agent_id, definition name, status, seconds). It is what emits
    # ``agent_done``.
    #
    # Blocking spawns fire it too, which they did not used to. The old rule --
    # a blocking child's completion *is* the spawner's tool result, so a second
    # marker is redundant -- holds for the transcript and for nothing else. The
    # roster, and anything replaying the log, had to infer the ending from "the
    # last thing I saw from this agent was an assistant_message", which is
    # simply wrong for every round that ends in tool calls: the provider emits
    # one ``TurnDone`` per round, so a busy child looks finished several times
    # before it is. An ending is a fact; it gets an event.
    on_done: Callable[[str, str, str, float], None] | None = None
    # Where a detached job's task goes to be owned. A task nobody holds is
    # garbage-collected at the end of the turn that created it, so this is not
    # bookkeeping -- it is the difference between a background job and a
    # cancelled one. None means this session cannot detach at all (see
    # ``BackgroundUnavailable``).
    adopt_task: Callable[[Any], None] | None = None
    # The agent these deps belong to -- the one that calls the ``agent`` tool,
    # and so the one to wake when one of its jobs finishes. Set per level, not
    # shared down: a nested agent's jobs are news for the nested agent.
    owner: AgentInstance | None = None
    # The tools this session actually has, including plugin and MCP ones. A
    # definition's ``tools:`` list is selected from this. None falls back to
    # the built-in core tools, which is what a bare embedder gets.
    tool_pool: list | None = None
    # Which agent definitions this session's preset admits (names or globs).
    # None means no restriction; an empty list means no delegation at all.
    # Superseded by ``parent.spawns``; kept for embedders that set it.
    allowed_agents: list[str] | None = None

    # -- composition ------------------------------------------------------
    # The session pool: everything this install has, minus the plugins that are
    # switched off. Set once at open and never narrowed on the way down -- it
    # is the *session's* capability envelope, not any one agent's grant, and
    # keeping the two apart is what makes "the orchestrator may not edit files,
    # but its children may" expressible at all.
    pool: list | None = None
    # The spawning agent's resolved composition. Children are intersected
    # against it from depth 1 down, so narrowing compounds instead of resetting.
    parent: Resolved | None = None
    # Agent definitions, snapshotted at session open. Reloading them per spawn
    # would change an agent's behaviour mid-conversation.
    defs: dict[str, AgentDef] | None = None
    # The session's preset, for the layer-3 contribution.
    preset: Any = None
    # Delegation turns spent per agent id, against that agent's max_turns.
    turns: dict[str, int] = field(default_factory=dict)
    budgets: dict[str, int] = field(default_factory=dict)
    # Which definition each spawned id came from. Shared down the tree like the
    # roster, so a resume three levels down can name the same definition in its
    # ``agent_done`` that the matching ``agent_spawned`` named.
    kinds: dict[str, str] = field(default_factory=dict)
    # Who spawned each id ("" for the session's own agent), shared down the
    # tree. The roster is shared so a resume can find a child from anywhere;
    # this is what stops "from anywhere" meaning "anyone": an agent may drive
    # and collect only what it, or an agent it spawned, started.
    spawners: dict[str, str] = field(default_factory=dict)
    # The id of the agent these deps belong to. Per level, never shared.
    self_id: str = ""
    # The session's frozen runtime numbers, shared down the whole tree so every
    # depth counts against the same budget the session opened with.
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    # The conversation's background shell jobs (``tools/bash_jobs.py``). Shared
    # down the tree like ``jobs``: one cap, and one table the conversation
    # closes, whichever agent started the command.
    bash_jobs: Any = None
    # The session's loop hooks. A child gets the user's command hooks for its
    # own tool calls (``hooks.child_hooks``); a guard that stopped at the
    # orchestrator would be one delegation away from not being a guard.
    hooks: list | None = None
    # Isolated children's git worktrees by agent id (``subagents/worktree.py``).
    # Shared down the tree like the roster: a resume three levels down reopens
    # the same checkout, and the conversation settles every one at close.
    worktrees: dict[str, Any] = field(default_factory=dict)
    # Where ``.quickcode/worktrees`` lives: the session's project. None means
    # ``cwd``, which is the project at depth 0; below that ``cwd`` may be an
    # isolated child's own worktree, and a worktree nested in another would be
    # deleted along with it.
    worktree_home: Path | None = None

    def child(self, depth: int, permissions: PermissionEngine,
              *, self_id: str, tool_pool: list | None = None,
              parent: Resolved | None = None, cwd: Path | None = None,
              env: Environment | None = None) -> SubagentDeps:
        """A deps object for the next level down, sharing the counter/roster.

        A child's own spawns are capped by the child's live mode and rules --
        read off its ``permissions``, never copied -- and narrowed against
        ``parent``, the composition this child was itself given. Passing the
        session's own composition down instead would make delegation an
        escalation: a read-only agent could spawn one whose definition says
        ``tools: null`` and have it inherit write, edit and bash.

        ``cwd`` and ``env`` are where the child works when that is not where
        its spawner does -- an isolated child's worktree -- so what it spawns
        starts there too.
        """
        return SubagentDeps(
            provider=self.provider,
            profile=self.profile,
            env=env if env is not None else self.env,
            mode_getter=lambda: permissions.mode,
            rules_getter=lambda: permissions.rules,
            cwd=cwd if cwd is not None else self.cwd,
            depth=depth,
            counter=self.counter,
            spawned=self.spawned,
            roster=self.roster,
            jobs=self.jobs,
            on_pane=self.on_pane,
            on_done=self.on_done,
            adopt_task=self.adopt_task,
            tool_pool=self.tool_pool if tool_pool is None else tool_pool,
            allowed_agents=self.allowed_agents,
            pool=self.pool,
            parent=parent if parent is not None else self.parent,
            defs=self.defs,
            preset=self.preset,
            turns=self.turns,
            budgets=self.budgets,
            kinds=self.kinds,
            spawners=self.spawners,
            self_id=self_id,
            limits=self.limits,
            bash_jobs=self.bash_jobs,
            hooks=self.hooks,
            worktrees=self.worktrees,
            worktree_home=self.worktree_home or self.cwd,
        )

    def close_worktrees(self) -> None:
        """Settle every isolated child's checkout still on disk and delete the
        branches already merged. Blocking; the conversation runs it off the
        event loop as it closes, after its tasks have stopped."""
        worktree.close_all(self.worktrees.values())

    def owns(self, agent_id: str) -> bool:
        """Whether this level, or an agent it spawned, started ``agent_id``."""
        seen: set[str] = set()
        spawner = self.spawners.get(agent_id)
        while spawner is not None and spawner not in seen:
            if spawner == self.self_id:
                return True
            seen.add(spawner)
            spawner = self.spawners.get(spawner)
        return False

    def visible_jobs(self) -> dict[str, JobRecord]:
        return {aid: job for aid, job in self.jobs.items() if self.owns(aid)}

    def session_pool(self) -> list:
        """The pool to resolve against, with the legacy fallbacks in order."""
        if self.pool is not None:
            return self.pool
        if self.tool_pool is not None:
            return self.tool_pool
        return core_tools(include_plan=False, include_agent=False)

    def definitions(self) -> dict[str, AgentDef]:
        return self.defs if self.defs is not None else load_defs(self.cwd)

    # -- detached jobs ----------------------------------------------------

    def background_available(self) -> bool:
        """Whether a detached job would have an owner to outlive the turn."""
        return self.adopt_task is not None

    def running_jobs(self) -> list[JobRecord]:
        return [j for j in self.jobs.values() if j.running]

    def uncollected_jobs(self) -> list[JobRecord]:
        """Finished jobs whose report the spawner has not read yet."""
        return [j for j in self.jobs.values() if not j.running and not j.collected]

    def cancel_jobs(self) -> int:
        """Cancel every job still in flight. Returns how many were cancelled.

        Called by the conversation on interrupt and on close. The tasks mark
        themselves ``cancelled`` as they unwind, so the registry stays truthful
        without this having to guess.
        """
        live = self.running_jobs()
        for job in live:
            if job.task is not None and not job.task.done():
                job.task.cancel()
        return len(live)
