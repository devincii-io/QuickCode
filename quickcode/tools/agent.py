"""The ``agent`` tool: delegate a self-contained task to a subagent.

The tool is stateless — it reads the ambient ``SubagentDeps`` from
``ctx.extra['subagent']`` (installed by the CLI for the main agent, and by the
runner for nested children) and hands off to ``spawn_subagent``. The subagent
runs to completion; only its sanitized final report returns to the parent.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from quickcode.tools.base import Tool, ToolCtx, ToolResult


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


class AgentTool(Tool[AgentInput]):
    name = "agent"
    description = (
        "Delegate a self-contained task to a subagent with fresh context. Use "
        "'explore' subagents (read-only) to fan out independent research in "
        "parallel, or 'general' for a bounded implementation/analysis task. The "
        "subagent sees only your prompt and returns a single final report. Prefer "
        "doing simple things yourself; delegate for context isolation or genuine "
        "parallelism. Give each subagent a distinct, non-overlapping scope."
    )
    # Mutating classification → the loop runs multiple agent calls sequentially,
    # preserving the single-writer principle for write-capable subagents.
    is_read_only = False
    Input = AgentInput

    def render_call(self, input: AgentInput) -> str:  # noqa: A002
        return f"⏺ agent[{input.agent_type}]: {input.description}"

    async def run(self, input: AgentInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        deps = ctx.extra.get("subagent")
        if deps is None:
            return ToolResult(
                "The agent tool is unavailable here (subagents cannot nest beyond "
                "the depth limit, or delegation is not configured).",
                is_error=True,
            )
        # Imported lazily to avoid a config/provider import at module load.
        from quickcode.subagents.runner import spawn_subagent

        try:
            agent_id, report = await spawn_subagent(
                deps,
                agent_type=input.agent_type,
                prompt=input.prompt,
                model_override=input.model,
            )
        except ValueError as e:
            return ToolResult(str(e), is_error=True)
        return ToolResult(f"<subagent id=\"{agent_id}\">\n{report}\n</subagent>")
