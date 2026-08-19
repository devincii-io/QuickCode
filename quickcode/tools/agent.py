"""The ``agent`` tool: delegate a self-contained task to a subagent.

The tool is stateless — it reads the ambient ``SubagentDeps`` from
``ctx.extra['subagent']`` (installed by the CLI for the main agent, and by the
runner for nested children) and hands off to ``spawn_subagent``. The subagent
runs to completion; only its sanitized final report returns to the parent.

With ``background: true`` the hand-off is to ``spawn_subagent_background``
instead: the child starts on a task the conversation owns and the tool returns
a job handle immediately, so the model keeps working and collects the report
later with ``agent_result``. Sessions with nothing to own such a task — a
headless ``-p`` run ends when its single turn does — run the delegation inline
and say so, rather than handing back a handle to a job that will be
garbage-collected before it finishes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from quickcode.subagents.jobs import DONE
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult


class AgentInput(BaseModel):
    description: str = Field(description="3-5 word label for this delegation (for the UI).")
    prompt: str = Field(
        description=(
            "The full task for the subagent — its ONLY context (it cannot see this "
            "conversation). Use the <task><objective><context><boundaries>"
            "<output_format></task> structure."
        )
    )
    agent_type: str = Field(
        default="general",
        description="Which agent definition to spawn: 'explore' (read-only), "
        "'general', or a custom name from .quickcode/agents/.",
    )
    model: str | None = Field(
        default=None,
        description="Optional model slug override (defaults to the definition's role).",
    )
    background: bool = Field(
        default=False,
        description=(
            "Start the subagent and return a job handle immediately instead of "
            "waiting for its report. Use it when you have other work to do "
            "meanwhile; you MUST collect the report later with agent_result."
        ),
    )


class AgentTool(Tool[AgentInput]):
    name = "agent"
    description = (
        "Delegate a self-contained task to a subagent with fresh context. Use "
        "'explore' subagents (read-only) to fan out independent research in "
        "parallel, or 'general' for a bounded implementation/analysis task. The "
        "subagent sees only your prompt and returns a single final report. Prefer "
        "doing simple things yourself; delegate for context isolation or genuine "
        "parallelism. Give each subagent a distinct, non-overlapping scope. The "
        "returned agent id can be resumed later via send_message instead of "
        "respawning a fresh subagent. Set background=true to get a job handle "
        "back at once and keep working — then collect the report with "
        "agent_result before you finish."
    )
    # Classified read-only so multiple spawns in one turn fan out CONCURRENTLY
    # (the loop batches read-only calls with asyncio.gather). Delegation is
    # read-only from the parent's view: the parent doesn't touch the filesystem
    # here — each child's own writes are gated by its capped permission mode and
    # deny-callback. Single-writer safety is the orchestrator's job (it's
    # prompted to give each subagent a distinct, non-overlapping file scope).
    is_read_only = True
    # Spawning a subagent touches nothing from the parent -- the child's own
    # actions are gated by its capped mode -- and a modal per spawn would make
    # fan-out unusable. Cost stays visible in the status meter.
    permission = PermissionSpec(mutates=False, target_field="agent_type")
    Input = AgentInput

    def render_call(self, input: AgentInput) -> str:  # noqa: A002
        suffix = " (background)" if input.background else ""
        return f"⏺ agent[{input.agent_type}]: {input.description}{suffix}"

    async def run(self, input: AgentInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        deps = ctx.extra.get("subagent")
        if deps is None:
            return ToolResult(
                "The agent tool is unavailable here (subagents cannot nest beyond "
                "the depth limit, or delegation is not configured).",
                is_error=True,
            )
        # Imported lazily to avoid a config/provider import at module load.
        from quickcode.subagents.runner import (
            BackgroundUnavailable,
            spawn_subagent,
            spawn_subagent_background,
        )

        inline_note = ""
        if input.background:
            try:
                job = spawn_subagent_background(
                    deps,
                    agent_type=input.agent_type,
                    prompt=input.prompt,
                    description=input.description,
                    model_override=input.model,
                )
            except BackgroundUnavailable:
                # Not an error the model can do anything about, and re-issuing
                # the call without the flag would cost a round trip to reach
                # the identical report. Run it inline and say which happened.
                inline_note = (
                    "\n(This session cannot detach subagent jobs, so this one ran "
                    "to completion inline. The report above is final — there is "
                    "nothing to collect.)"
                )
            except ValueError as e:
                return ToolResult(str(e), is_error=True)
            else:
                return ToolResult(
                    f"{job.to_tag()}\nStarted in the background. Carry on with other "
                    f'work, then call agent_result with agent_id="{job.agent_id}" to '
                    "read the report. Do not end your turn with it uncollected."
                )

        try:
            agent_id, report, status = await spawn_subagent(
                deps,
                agent_type=input.agent_type,
                prompt=input.prompt,
                model_override=input.model,
            )
        except ValueError as e:
            return ToolResult(str(e), is_error=True)
        # A child that errored or was cut off comes back with a report like any
        # other, and calling that a success put a green tick and "Status: ok" on
        # the parent's tool card over a subagent that had died. The status
        # `_run_and_finish` already decided is the honest answer, and naming it
        # in the tag lets the roster settle the child's row on a fact rather
        # than on a guess about the report's wording.
        return ToolResult(
            f'<subagent id="{agent_id}" status="{status}">\n{report}\n</subagent>{inline_note}',
            is_error=status != DONE,
        )
