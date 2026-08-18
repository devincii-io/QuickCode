"""The ``send_message`` tool: resume a previously spawned subagent.

Companion to the ``agent`` tool — instead of spawning a fresh child with an
empty history, this sends a follow-up message to one already in the shared
roster, resuming it with its full context (history, prior tool state) intact.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult


class SendMessageInput(BaseModel):
    agent_id: str = Field(
        description="The id of a previously spawned subagent, as returned by the agent tool "
        "(e.g. 'explore-1')."
    )
    message: str = Field(
        description="The follow-up message for the subagent. It sees this in addition to its "
        "full prior context — no need to repeat earlier instructions."
    )


class SendMessageTool(Tool[SendMessageInput]):
    name = "send_message"
    description = (
        "Send a follow-up message to a previously spawned subagent, resuming it with its "
        "full context intact. Prefer this over respawning when iterating on the same task. "
        "The agent_id is the id returned by the agent tool."
    )
    # Classified read-only for the same reason as the agent tool: sending a
    # message doesn't touch the filesystem from the parent's view (the child's
    # own actions are gated by its capped mode), and concurrent fan-out can't
    # surface a separate modal per message.
    is_read_only = True
    permission = PermissionSpec(mutates=False, target_field="agent_id")
    Input = SendMessageInput

    def render_call(self, input: SendMessageInput) -> str:  # noqa: A002
        return f"⏺ send_message[{input.agent_id}]"

    async def run(self, input: SendMessageInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        deps = ctx.extra.get("subagent")
        if deps is None:
            return ToolResult(
                "The send_message tool is unavailable here (subagents cannot nest beyond "
                "the depth limit, or delegation is not configured).",
                is_error=True,
            )
        # Imported lazily to avoid a config/provider import at module load.
        from quickcode.subagents.runner import resume_subagent

        try:
            agent_id, report = await resume_subagent(
                deps, agent_id=input.agent_id, message=input.message
            )
        except ValueError as e:
            return ToolResult(str(e), is_error=True)
        return ToolResult(f"<subagent id=\"{agent_id}\">\n{report}\n</subagent>")
