"""Tool base class, result type, and execution context.

Every tool takes a Pydantic input model (→ strict JSON Schema on the wire),
runs against a ``ToolCtx``, and returns a ``ToolResult``. Truncation happens
*inside* the tool with an explicit marker the model can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from quickcode.providers.base import ToolSchema

In = TypeVar("In", bound=BaseModel)


@dataclass
class ReadRegistry:
    """Tracks {path: mtime} for files read this session (edit staleness check)."""

    seen: dict[str, float] = field(default_factory=dict)

    def record(self, path: str, mtime: float) -> None:
        self.seen[str(Path(path))] = mtime

    def mtime_at_read(self, path: str) -> float | None:
        return self.seen.get(str(Path(path)))

    def was_read(self, path: str) -> bool:
        return str(Path(path)) in self.seen


@dataclass
class ToolCtx:
    """Ambient state a tool may need. Passed to every ``run``."""

    cwd: Path
    read_registry: ReadRegistry
    shell_name: str = "bash"
    platform: str = "win32"
    # Set by the bash tool to persist a shell session per conversation.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """What a tool returns. ``content`` is the string the model sees."""

    content: str
    is_error: bool = False
    ui_meta: dict[str, Any] = field(default_factory=dict)


def truncate(text: str, limit: int, *, hint: str = "") -> str:
    """Cap text with an explicit marker the model can act on."""
    if len(text) <= limit:
        return text
    shown = text[:limit]
    extra = f' hint="{hint}"' if hint else ""
    return f'{shown}\n<truncated shown="{limit}" total="{len(text)}"{extra}/>'


class Tool(Generic[In]):
    """Base class for all tools.

    Subclasses set ``name``, ``description``, ``Input`` and ``is_read_only`` and
    implement ``run``. ``render_call`` / ``render_result`` return terminal-ready
    strings for the transcript (the UI may wrap them in widgets).
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    is_read_only: ClassVar[bool] = False
    Input: type[BaseModel] = BaseModel

    async def run(self, input: In, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        raise NotImplementedError

    # --- rendering (plain-string defaults; UI may override with widgets) ---
    def render_call(self, input: In) -> str:  # noqa: A002
        return f"⏺ {self.name}"

    def render_result(self, result: ToolResult) -> str:
        return result.content

    # --- wire schema ---
    def schema(self) -> ToolSchema:
        params = self.Input.model_json_schema()
        params["additionalProperties"] = False
        return ToolSchema(name=self.name, description=self.description, parameters=params)
