"""Tool base class, result type, and execution context.

Every tool takes a Pydantic input model (→ strict JSON Schema on the wire),
runs against a ``ToolCtx``, and returns a ``ToolResult``. Truncation happens
*inside* the tool with an explicit marker the model can act on.
"""

from __future__ import annotations

import contextlib
import locale
import re
from dataclasses import dataclass, field
from functools import lru_cache
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


# A lone surrogate is what ``errors="surrogateescape"`` leaves behind for a
# byte that is not valid UTF-8. It survives inside a Python string and then
# fails the moment anything encodes it -- and everything downstream does:
# `json.dumps(..., ensure_ascii=False).encode("utf-8")` is both the session
# log write and the WebSocket frame, and the provider request is a third.
_SURROGATES = re.compile("[\ud800-\udfff]")


def clean_text(text: str) -> str:
    """Text that is safe to log, broadcast and send to a model.

    Every tool result passes through here. A tool that hands back bytes which
    were never UTF-8 used to kill the turn with ``UnicodeEncodeError:
    surrogates not allowed`` -- raised not in the tool but in the recorder,
    long after the tool returned, so the command showed as still running and
    the turn never ended.

    Replacing rather than raising is the choice a transcript invites: the
    output is still worth reading with one character replaced, and a tool is
    not the place to litigate somebody else's encoding.
    """
    return _SURROGATES.sub("�", text) if text else text


def decode_output(raw: bytes) -> str:
    """Bytes from a child process, as text.

    UTF-8 first, because that is what almost everything emits, and because it
    is the one encoding here that can *reject* input: a byte sequence that
    decodes as UTF-8 almost certainly is UTF-8.

    After that, the machine's own code page, once. On a German Windows
    ``Größe`` arrives from a console program as cp1252 bytes, and reading it as
    UTF-8 with replacements turns a word the user can read into one they
    cannot.

    Only one fallback is tried, deliberately. A single-byte code page maps
    every one of its 256 bytes, so it never raises -- a chain of them would not
    be choosing the right encoding, it would be returning whichever happened to
    be first while looking like it had decided something. There is no way to
    tell cp1252 from cp850 by inspection, so this picks the system's and says
    so rather than dressing the guess up.

    Never ``surrogateescape``: that defers the failure to whoever encodes the
    string next, which is how a bad byte in a command's output came to kill a
    turn inside the recorder long after the tool had returned.
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    encoding = _system_encoding()
    if encoding:
        with contextlib.suppress(UnicodeDecodeError, LookupError):
            return raw.decode(encoding)
    return raw.decode("utf-8", errors="replace")


@lru_cache(maxsize=1)
def _system_encoding() -> str:
    """The machine's own code page, or "" if it is UTF-8 (already tried)."""
    try:
        name = locale.getpreferredencoding(False)
    except Exception:  # noqa: BLE001 - a locale this odd is not worth a crash
        return ""
    return "" if name.lower().replace("-", "") in {"utf8", "utf8mb4"} else name


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
