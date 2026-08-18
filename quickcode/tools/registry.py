"""Tool registry: the set of tools exposed to the model.

There is one catalogue of core tools here and one way to select from it.
There used to be three -- a factory map for subagents, a builder for the main
agent, and a name allowlist in the subagent definitions -- which is why a
subagent could never be granted a plugin or MCP tool: it was selecting from a
list those tools were never in. Selection now runs against whatever pool the
caller passes, so a definition can say ``tools: [read, grep, mcp__*]`` and
mean it.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

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
from quickcode.tools.web_fetch import WebFetchTool
from quickcode.tools.web_search import WebSearchTool
from quickcode.tools.write import WriteTool

# Selection aliases: a short word in a definition's ``tools:`` list that stands
# for a family. Everything else is matched by name or glob.
ALIASES: dict[str, str] = {"task": "task_*"}


class ToolRegistry:
    """Holds the tools available in a session, keyed by name."""

    def __init__(self, tools: list[Tool]) -> None:
        self.tools: dict[str, Tool] = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [t.schema() for t in self.tools.values()]

    def permission_specs(self) -> dict[str, object]:
        """Per-tool permission shapes, for a ``PermissionEngine``."""
        from quickcode.core.permissions import DEFAULT_SPEC

        return {name: getattr(t, "permission", DEFAULT_SPEC) for name, t in self.tools.items()}


def core_tools(*, include_plan: bool = True, include_agent: bool = True) -> list[Tool]:
    """Fresh instances of every tool QuickCode ships.

    ``plan`` is for interactive sessions only -- a subagent has no one to show
    a plan to. ``agent``/``send_message`` are the delegation pair, withheld at
    the depth floor.
    """
    tools: list[Tool] = [
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        # Registered whether or not a search key is configured: an unconfigured
        # web_search fails with the signup page in the message, which is more
        # use to everyone than a tool that silently does not exist. Same
        # reasoning as the OpenRouter key -- see tools/web_search.py.
        WebFetchTool(),
        WebSearchTool(),
        *task_tools(),
    ]
    if include_plan:
        tools.append(PlanTool())
    if include_agent:
        tools.append(AgentTool())
        tools.append(SendMessageTool())
    return tools


def select(pool: Iterable[Tool], patterns: Iterable[str]) -> list[Tool]:
    """Tools from ``pool`` matching any name, alias or glob in ``patterns``.

    Order follows the pool, not the patterns, so a registry is deterministic
    however the allowlist was written. A pattern matching nothing is silently
    empty: an allowlist mentioning a tool this install doesn't have should
    yield a smaller agent, not a crash.
    """
    wanted = [ALIASES.get(p.strip(), p.strip()) for p in patterns if p and p.strip()]
    out: list[Tool] = []
    for tool in pool:
        if any(tool.name == p or fnmatchcase(tool.name, p) for p in wanted):
            out.append(tool)
    return out


def default_registry(*, include_agent: bool = True) -> ToolRegistry:
    """The standard toolset for a main agent."""
    return ToolRegistry(core_tools(include_plan=True, include_agent=include_agent))


def build_registry(
    tool_names: list[str] | None,
    *,
    include_agent: bool = False,
    pool: Iterable[Tool] | None = None,
) -> ToolRegistry:
    """Build a bounded registry for a subagent.

    ``tool_names=None`` inherits the whole pool. ``pool`` defaults to the core
    tools; callers with a live session registry pass its tools so plugin and
    MCP tools are grantable too. ``plan`` is never included.
    """
    available = list(pool) if pool is not None else core_tools(
        include_plan=False, include_agent=False
    )
    available = [t for t in available if t.name != "plan"]
    # The delegation pair is granted by depth, not by the allowlist, so it is
    # never selectable and never inherited.
    delegation = {"agent", "send_message"}
    available = [t for t in available if t.name not in delegation]

    chosen = available if tool_names is None else select(available, tool_names)
    if include_agent:
        chosen = [*chosen, AgentTool(), SendMessageTool()]
    return ToolRegistry(chosen)
