"""Tool registry: the set of tools exposed to the model."""

from __future__ import annotations

from quickcode.providers.base import ToolSchema
from quickcode.tools.agent import AgentTool
from quickcode.tools.base import Tool
from quickcode.tools.bash import BashTool
from quickcode.tools.edit import EditTool
from quickcode.tools.glob import GlobTool
from quickcode.tools.grep import GrepTool
from quickcode.tools.plan import PlanTool
from quickcode.tools.read import ReadTool
from quickcode.tools.send_message import SendMessageTool
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


# Factories for the single-instance core tools, keyed by the name a definition
# would use in its ``tools:`` allowlist. ``task`` expands to the whole board set.
def _core_tool_factories() -> dict[str, list[Tool]]:
    return {
        "read": [ReadTool()],
        "write": [WriteTool()],
        "edit": [EditTool()],
        "glob": [GlobTool()],
        "grep": [GrepTool()],
        "bash": [BashTool()],
        "task": list(task_tools()),
    }


def default_registry(*, include_agent: bool = True) -> ToolRegistry:
    """The standard core tools: file/shell tools, the task board, plan, and
    (for the main agent) the ``agent``/``send_message`` delegation tools."""
    tools: list[Tool] = [
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        *task_tools(),
        PlanTool(),
    ]
    if include_agent:
        tools.append(AgentTool())
        tools.append(SendMessageTool())
    return ToolRegistry(tools)


def build_registry(
    tool_names: list[str] | None, *, include_agent: bool = False
) -> ToolRegistry:
    """Build a bounded registry from an allowlist of core tool names.

    ``tool_names=None`` inherits the full core toolset. ``plan`` is never
    included (subagents don't do interactive plan review). ``include_agent``
    grants the delegation tools (``agent`` and ``send_message``) when the
    child is still above the depth floor.
    """
    factories = _core_tool_factories()
    names = list(factories) if tool_names is None else tool_names
    tools: list[Tool] = []
    for n in names:
        tools.extend(factories.get(n, []))
    if include_agent:
        tools.append(AgentTool())
        tools.append(SendMessageTool())
    return ToolRegistry(tools)
