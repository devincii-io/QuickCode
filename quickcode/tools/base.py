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

from quickcode.core.permissions import DEFAULT_SPEC, PermissionSpec
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

    ``permission`` tells the permission engine how to gate this tool: whether
    it mutates, which argument is the thing being acted on, and whether that
    argument is a path or a shell command. Declaring it here is what lets a
    plugin tool get the same protection a built-in one gets -- the engine no
    longer recognises tools by name. The default is the cautious one: an
    undeclared tool is treated as mutating and is prompted for.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    is_read_only: ClassVar[bool] = False
    # Whether Stop may cut this tool off mid-run. Off by default, and that is
    # the safe default rather than a shrug: killing `write` or `edit` halfway
    # through trades a slow interrupt for a truncated file. A tool sets this
    # when it can be stopped without leaving a mess -- and when it owns the
    # child process it must kill on the way out, which `run` does by catching
    # CancelledError. The one that matters is `bash`: without it, Stop cannot
    # end a `find /` and the turn sits there until the command's own timeout.
    interruptible: ClassVar[bool] = False
    permission: ClassVar[PermissionSpec] = DEFAULT_SPEC
    # Where this tool came from: "internal" (shipped), "entrypoint" (a plugin
    # package) or "config" (an MCP server). Carried on the tool rather than
    # inferred from its name, so the Settings list stays truthful when a
    # plugin ships a tool called "read".
    source: ClassVar[str] = "internal"
    # Where the definition lives, for tools that have a file behind them (an
    # authored command tool). Empty for tools that are code.
    path: ClassVar[str] = ""
    Input: type[BaseModel] = BaseModel

    async def run(self, input: In, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        raise NotImplementedError

    def permission_target(self, args: dict) -> str:
        """The location this call really acts on, when one field cannot say it.

        ``PermissionSpec.target_field`` names a single argument, which is right
        for nearly every tool. ``glob`` is the exception that proves why the
        hook exists: it gates ``path``, an *optional* field, while the place it
        actually reads is ``path`` joined with ``pattern`` -- so
        ``glob(pattern="../*/*.txt")`` presented an empty target and walked out
        of the project unprompted. Returning "" means "use the declared field".
        """
        return ""

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
