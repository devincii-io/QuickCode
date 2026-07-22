"""Tool registry: the set of tools exposed to the model."""

from __future__ import annotations

from quickcode.providers.base import ToolSchema
from quickcode.tools.base import Tool
from quickcode.tools.bash import BashTool
from quickcode.tools.edit import EditTool
from quickcode.tools.glob import GlobTool
from quickcode.tools.grep import GrepTool
from quickcode.tools.plan import PlanTool
from quickcode.tools.read import ReadTool
from quickcode.tools.task import task_tools
from quickcode.tools.write import WriteTool


class ToolRegistry:
    """Holds the tools available in a session, keyed by name."""

    def __init__(self, tools: list[Tool]) -> None:
        self.tools: dict[str, Tool] = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [t.schema() for t in self.tools.values()]


def default_registry() -> ToolRegistry:
    """The standard core tools: six file/shell tools, the task board, and plan."""
    return ToolRegistry(
        [
            ReadTool(),
            WriteTool(),
            EditTool(),
            GlobTool(),
            GrepTool(),
            BashTool(),
            *task_tools(),
            PlanTool(),
        ]
    )
