"""``agent_status`` and ``agent_result``: the other half of a detached spawn.

``agent(background=true)`` returns a handle and nothing else. These two are how
that handle becomes an answer — one asks what is in flight, the other reads a
finished job's report. Both are stateless in the same way the ``agent`` tool
is: the registry they read lives on the ambient ``SubagentDeps``, shared across
the whole conversation's agent tree, so a job started at any depth is visible
to the agent that started it.

The report ``agent_result`` returns has already been sanitized and offloaded by
``_run_and_finish`` — a detached run and a blocking one go through the same
finishing path, so collecting one here is exactly as safe as reading the
``agent`` tool's own result.
"""

from __future__ import annotations

import asyncio
import contextlib

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult

# The longest ``agent_result`` will hold a turn open waiting for a job. Long
# enough to be a real "join now" (a research subagent is minutes, not seconds),
# short enough that a wedged child cannot hold the conversation for ever.
MAX_WAIT_S = 600.0

_UNAVAILABLE = (
    "This tool is unavailable here (subagents cannot nest beyond the depth "
    "limit, or delegation is not configured)."
)


def _jobs(ctx: ToolCtx):
    """The conversation's job registry, or None when there is no delegation."""
    deps = ctx.extra.get("subagent")
    return None if deps is None else deps.jobs


class AgentStatusInput(BaseModel):
    agent_id: str | None = Field(
        default=None,
        description=(
            "One job's id, as returned by agent(background=true). Omit to list "
            "every background job in this conversation."
        ),
    )


class AgentStatusTool(Tool[AgentStatusInput]):
    name = "agent_status"
    description = (
        "Report the state of the background subagent jobs started with "
        "agent(background=true): running, done, error or cancelled, and whether "
        "you have collected each report yet. Omit agent_id to list them all. "
        "Cheap — use it to check what is still in flight before you finish, and "
        "agent_result to actually read a finished job's report."
    )
    # Reading a registry this conversation owns: no filesystem, no network, no
    # model call. Read-only so it batches with the other reads in a round.
    is_read_only = True
    permission = PermissionSpec(mutates=False, target_field="agent_id")
    Input = AgentStatusInput

    def render_call(self, input: AgentStatusInput) -> str:  # noqa: A002
        return f"⏺ agent_status[{input.agent_id}]" if input.agent_id else "⏺ agent_status"

    async def run(self, input: AgentStatusInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        jobs = _jobs(ctx)
        if jobs is None:
            return ToolResult(_UNAVAILABLE, is_error=True)

        if input.agent_id:
            job = jobs.get(input.agent_id)
            if job is None:
                known = ", ".join(jobs) or "(none)"
                return ToolResult(
                    f"unknown background job '{input.agent_id}'. Known jobs: {known}",
                    is_error=True,
                )
            return ToolResult(job.to_tag())

        if not jobs:
            return ToolResult(
                "<agent_jobs count=\"0\"/>\nNo background jobs have been started "
                "in this conversation."
            )
        running = sum(1 for j in jobs.values() if j.running)
        uncollected = sum(1 for j in jobs.values() if not j.running and not j.collected)
        lines = [
            f'<agent_jobs count="{len(jobs)}" running="{running}" '
            f'uncollected="{uncollected}">',
            *(job.to_tag() for job in jobs.values()),
            "</agent_jobs>",
        ]
        return ToolResult("\n".join(lines))


class AgentResultInput(BaseModel):
    agent_id: str = Field(
        description="The id of a background job, as returned by agent(background=true)."
    )
    wait_s: float = Field(
        default=0,
        description=(
            "Seconds to wait if the job is still running. 0 (the default) returns "
            "the current state at once; a positive value blocks until the job "
            f"finishes or the wait runs out (max {int(MAX_WAIT_S)})."
        ),
    )


class AgentResultTool(Tool[AgentResultInput]):
    name = "agent_result"
    description = (
        "Collect the report of a subagent started with agent(background=true). "
        "Returns the same sanitized final report a blocking agent call would have "
        "returned. If the job is still running you are told so and no report is "
        "returned — pass wait_s to block for it instead of polling in a loop."
    )
    # Same reasoning as agent/send_message: reading a report the harness already
    # sanitized touches nothing from the collector's side, and a modal here
    # would be a modal on work the user already approved when it was spawned.
    is_read_only = True
    permission = PermissionSpec(mutates=False, target_field="agent_id")
    Input = AgentResultInput

    def render_call(self, input: AgentResultInput) -> str:  # noqa: A002
        return f"⏺ agent_result[{input.agent_id}]"

    async def run(self, input: AgentResultInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        jobs = _jobs(ctx)
        if jobs is None:
            return ToolResult(_UNAVAILABLE, is_error=True)
        job = jobs.get(input.agent_id)
        if job is None:
            known = ", ".join(jobs) or "(none)"
            return ToolResult(
                f"unknown background job '{input.agent_id}'. Known jobs: {known}. "
                "A blocking agent call has no job record — its report was its "
                "tool result.",
                is_error=True,
            )

        wait = max(0.0, min(float(input.wait_s or 0), MAX_WAIT_S))
        if job.running and wait and job.task is not None:
            # ``asyncio.wait`` returns on timeout instead of raising, and does
            # not re-raise the task's own exception into this coroutine -- both
            # of which are what a collector wants: the JobRecord already holds
            # whatever happened.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({job.task}, timeout=wait)

        if job.running:
            return ToolResult(
                f"{job.to_tag()}\nStill running after {job.seconds()}s — there is no "
                "report yet. Do something else and call agent_result again, or pass "
                "wait_s to wait for it."
            )

        job.collected = True
        return ToolResult(
            f'<subagent id="{job.agent_id}" status="{job.status}">\n'
            f"{job.report}\n</subagent>"
        )
