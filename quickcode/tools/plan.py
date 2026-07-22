"""The `plan` tool: present a plan for approval and exit plan mode.

The tool itself only declares the schema/description the model sees. The actual
approval interaction (PlanReviewModal) is handled by the agent loop, which
intercepts calls to this tool and routes them to ``agent.plan_cb`` — the same
pattern as permission prompts. ``run`` is therefore never called in practice,
but is implemented as a safe fallback.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from quickcode.tools.base import Tool, ToolCtx, ToolResult


class PlanInput(BaseModel):
    plan: str = Field(description="The proposed plan as markdown, ready for the user to review.")


class PlanTool(Tool[PlanInput]):
    name = "plan"
    description = (
        "Present a plan for the user to approve, then exit plan mode. Call this "
        "only when you are in plan mode and have finished investigating: pass the "
        "full plan as markdown. The user reviews it and chooses to approve "
        "(execution proceeds) or send feedback (you keep planning). Do not use "
        "this to ask questions — it is for a complete, actionable plan."
    )
    is_read_only = False
    Input = PlanInput

    async def run(self, input: PlanInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        # The loop intercepts `plan`; this is only reached if plan approval is
        # unavailable (e.g. headless). Treat it as accepted-with-no-modal.
        return ToolResult(content="Plan recorded.", ui_meta={"plan": input.plan})

    def render_call(self, input: PlanInput) -> str:  # noqa: A002
        return "⏺ plan (awaiting review)"
