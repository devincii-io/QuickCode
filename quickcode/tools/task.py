"""Task board tools: task_create, task_update, task_list, task_get.

Solo checklist today, teammate coordination backbone later (docs/AGENTS.md
§3) — same four ops either way. All four operate on a single ``TaskBoard``
stashed in ``ctx.extra["task_board"]`` so it persists across tool calls
within a session; the first call that doesn't find one creates it.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from quickcode.core.tasks import TaskBoard
from quickcode.tools.base import PermissionSpec, Tool, ToolCtx, ToolResult


def _board(ctx: ToolCtx) -> TaskBoard:
    board = ctx.extra.get("task_board")
    if board is None:
        board = TaskBoard()
        ctx.extra["task_board"] = board
    return board


class TaskCreateInput(BaseModel):
    subject: str = Field(..., description="Short imperative task title.")
    description: str = Field("", description="Longer description of the task.")
    active_form: str = Field(
        "", description="Present-continuous label shown while in progress, e.g. 'Fixing auth bug'."
    )


class TaskCreateTool(Tool[TaskCreateInput]):
    name: ClassVar[str] = "task_create"
    description: ClassVar[str] = (
        "Creates a new task on the task board and returns its assigned id. "
        "Use for 3+ step work to track progress; the harness assigns ids (T1, T2, ...)."
    )
    is_read_only: ClassVar[bool] = False
    # The task board is QuickCode's own bookkeeping, not the user's project:
    # writing to it never needs a prompt, whatever the mode.
    permission = PermissionSpec(mutates=False)
    Input = TaskCreateInput

    def render_call(self, input: TaskCreateInput) -> str:  # noqa: A002
        return f"⏺ task_create: {input.subject}"

    async def run(self, input: TaskCreateInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        board = _board(ctx)
        task = board.create(
            subject=input.subject,
            description=input.description,
            active_form=input.active_form,
        )
        # The board changed: the UI's task panel refreshes off this flag rather
        # than off the tool's name.
        return ToolResult(
            content=f"Created {task.id}: {task.subject}",
            ui_meta={"tasks_changed": True},
        )


class TaskUpdateInput(BaseModel):
    task_id: str = Field(..., description="Id of the task to update, e.g. 'T3'.")
    status: Literal["pending", "in_progress", "completed", "deleted"] | None = Field(
        None, description="New status for the task."
    )
    owner: str | None = Field(None, description="Name of the agent/teammate claiming the task.")
    add_blocked_by: list[str] = Field(
        default_factory=list, description="Task ids that must complete before this one can start."
    )
    add_blocks: list[str] = Field(
        default_factory=list, description="Task ids that this task blocks."
    )


class TaskUpdateTool(Tool[TaskUpdateInput]):
    name: ClassVar[str] = "task_update"
    description: ClassVar[str] = (
        "Updates a task's status, owner, or dependency edges. Setting status to "
        "in_progress is rejected if any blocked_by task is not completed."
    )
    is_read_only: ClassVar[bool] = False
    permission = PermissionSpec(mutates=False, target_field="task_id")
    Input = TaskUpdateInput

    def render_call(self, input: TaskUpdateInput) -> str:  # noqa: A002
        return f"⏺ task_update: {input.task_id}"

    async def run(self, input: TaskUpdateInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        board = _board(ctx)
        try:
            task = board.update(
                input.task_id,
                status=input.status,
                owner=input.owner,
                add_blocked_by=input.add_blocked_by or None,
                add_blocks=input.add_blocks or None,
            )
        except KeyError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)
        except ValueError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)
        return ToolResult(
            content=f"Updated {task.id}: status={task.status} owner={task.owner}",
            ui_meta={"tasks_changed": True},
        )


class TaskListInput(BaseModel):
    pass


class TaskListTool(Tool[TaskListInput]):
    name: ClassVar[str] = "task_list"
    description: ClassVar[str] = "Lists all non-deleted tasks on the task board as a checklist."
    is_read_only: ClassVar[bool] = True
    permission = PermissionSpec(mutates=False)
    Input = TaskListInput

    def render_call(self, input: TaskListInput) -> str:  # noqa: A002
        return "⏺ task_list"

    async def run(self, input: TaskListInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        board = _board(ctx)
        return ToolResult(content=board.render_checklist())


class TaskGetInput(BaseModel):
    task_id: str = Field(..., description="Id of the task to fetch, e.g. 'T3'.")


class TaskGetTool(Tool[TaskGetInput]):
    name: ClassVar[str] = "task_get"
    description: ClassVar[str] = "Fetches full detail for a single task by id."
    is_read_only: ClassVar[bool] = True
    permission = PermissionSpec(mutates=False, target_field="task_id")
    Input = TaskGetInput

    def render_call(self, input: TaskGetInput) -> str:  # noqa: A002
        return f"⏺ task_get: {input.task_id}"

    async def run(self, input: TaskGetInput, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        board = _board(ctx)
        try:
            task = board.get(input.task_id)
        except KeyError as exc:
            return ToolResult(content=f"Error: {exc}", is_error=True)
        lines = [
            f"id: {task.id}",
            f"subject: {task.subject}",
            f"description: {task.description}",
            f"active_form: {task.active_form}",
            f"status: {task.status}",
            f"owner: {task.owner}",
            f"blocked_by: {', '.join(task.blocked_by) or '(none)'}",
            f"blocks: {', '.join(task.blocks) or '(none)'}",
        ]
        return ToolResult(content="\n".join(lines))


def task_tools() -> list[Tool]:
    """The four task-board tools, ready to register."""
    return [TaskCreateTool(), TaskUpdateTool(), TaskListTool(), TaskGetTool()]
