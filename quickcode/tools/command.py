"""Command tools: a narrowed ``bash``, authored in markdown, executed as argv.

A command tool turns "I know the command" into "the agent knows the command",
with a name, a description written for this project, typed parameters the model
can reason about, and an approval prompt that shows the exact argv before
anything runs.

**Argv, never a shell.** The template is a JSON array; each element is one
token; the process is spawned with ``asyncio.create_subprocess_exec``. A
parameter value containing ``; rm -rf /`` or ``$(curl evil)`` is inert bytes,
because nothing between the model and ``execve`` ever parses it. There is no
sanitiser here to be wrong about a case nobody thought of -- injection is
structurally impossible rather than filtered. That is defence against the
*model*, which fills the parameters and is the one component in this path
nobody can audit. Shell mode is refused at validation, not half-implemented.

**Permission.** A command tool declares ``PermissionSpec(mutates=True)``,
always. ``read_only: true`` in the frontmatter is recorded and surfaced, and
grants nothing: QuickCode cannot check what a program does, and an authored
file that could opt itself out of the permission prompt would be a hole exactly
as large as an unaudited MCP server with a nicer card. The way to stop being
asked is a permission rule in ``settings.json`` -- a decision the user makes,
in the place the other such decisions are visible. ``permission_target`` is
honoured because it can only make a rule *more* specific.

**Path parameters** are resolved against the session cwd and refused if they
leave the project root or name a secret-bearing file. That refusal lives in the
tool rather than in the gate, and it is not configurable: a command tool must
not become the way to read ``~/.ssh``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, create_model

from quickcode.context import toon
from quickcode.core.permissions import PermissionSpec
from quickcode.kernel.authoring import argv as argv_rules
from quickcode.kernel.authoring.model import AuthoredPlugin, Param
from quickcode.tools.base import Tool, ToolCtx, ToolResult, decode_output, truncate

# Environment handed to the child. A command tool is started from a file that
# may be committed, so the child gets what a program needs to run and not the
# whole ambient environment: an API key in ``os.environ`` is not something a
# repository's tool should inherit by default. Anything else is opt-in through
# ``env_from``.
_BASE_ENV_KEYS = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LANG", "LC_ALL",
    "TZ", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS",
    "PYTHONIOENCODING", "TERM",
)

_SECRET_PARTS = re.compile(r"^(\.ssh|\.env(\..+)?)$")


class CommandTool(Tool[BaseModel]):
    """One authored command, presented to the model as an ordinary tool."""

    is_read_only: ClassVar[bool] = False
    source: ClassVar[str] = "authored"

    def __init__(self, plugin: AuthoredPlugin) -> None:
        self.plugin = plugin
        self.name = plugin.name
        self.description = _describe(plugin)
        self.path = plugin.path
        self.Input = _input_model(plugin)
        target = plugin.permission_target
        param = plugin.params_by_name().get(target) if target else None
        self.permission = PermissionSpec(
            # Always mutating. See the module docstring: a declaration in a file
            # is a claim, and the only check available for "does this program
            # write?" is that there is none.
            mutates=True,
            target_field=target or None,
            path_target=bool(param is not None and param.type == "path"),
            shell=False,
        )

    # -- transcript -------------------------------------------------------

    def render_call(self, input: BaseModel) -> str:  # noqa: A002
        values = input.model_dump()
        if self.plugin.label:
            return f"⏺ {argv_rules.render_element(self.plugin.label, values)}"
        try:
            resolved = self.resolve_argv(values)
        except Exception:
            return f"⏺ {self.name}"
        return "⏺ " + " ".join(resolved)

    # -- the argv ---------------------------------------------------------

    def resolve_argv(self, values: dict[str, Any]) -> list[str]:
        return argv_rules.render_argv(
            self.plugin.argv, self.plugin.params_by_name(), values
        )

    # -- execution --------------------------------------------------------

    async def run(self, input: BaseModel, ctx: ToolCtx) -> ToolResult:  # noqa: A002
        plugin = self.plugin
        values = input.model_dump()
        root = Path(ctx.cwd).resolve()

        refusal = _check_paths(plugin, values, root)
        if refusal:
            return ToolResult(content=refusal, is_error=True)

        try:
            argv = self.resolve_argv(values)
        except Exception as exc:  # a template that survived validation is rare
            return ToolResult(content=f"Error: could not build the command: {exc}",
                              is_error=True)
        if not argv:
            return ToolResult(content="Error: the command resolved to nothing.",
                              is_error=True)

        workdir = _workdir(plugin, values, root)
        env = _child_env(plugin)
        meta = {"argv": list(argv), "cwd": str(workdir), "tool": self.name,
                "authored": True, "path": plugin.path}

        combined = plugin.output == "text"
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if combined else asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if plugin.stdin else asyncio.subprocess.DEVNULL,
                env=env,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                content=f"Error: could not start {argv[0]!r}: {exc}",
                is_error=True, ui_meta=meta,
            )

        payload = plugin.stdin.encode("utf-8") if plugin.stdin else None
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload), timeout=plugin.timeout_ms / 1000.0
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return ToolResult(
                content=f"Error: {self.name} timed out after {plugin.timeout_ms}ms "
                        "and was killed.",
                is_error=True, ui_meta=meta,
            )

        code = proc.returncode if proc.returncode is not None else -1
        stdout = _decode(out)
        stderr = _decode(err)
        meta["exit_code"] = code
        ok = code in plugin.success_exit_codes

        if not ok and plugin.on_nonzero == "error":
            tail = stdout if combined else "\n".join(t for t in (stdout, stderr) if t)
            body = truncate(tail.strip(), plugin.max_output_chars,
                            hint="rerun with narrower arguments")
            content = f"{self.name} exited with code {code}."
            return ToolResult(content=f"{content}\n{body}" if body else content,
                              is_error=True, ui_meta=meta)

        return _map_output(plugin, stdout, stderr, code, ok, meta)


# --------------------------------------------------------------------------
# input model
# --------------------------------------------------------------------------

_PY_TYPES: dict[str, Any] = {
    "string": str, "text": str, "path": str,
    "int": int, "float": float, "bool": bool,
}


def _field_type(param: Param) -> Any:
    if param.type == "enum":
        return Literal[tuple(param.choices)]  # type: ignore[valid-type]
    if param.type == "list":
        return list[_PY_TYPES.get(param.item_type, str)]
    return _PY_TYPES.get(param.type, str)


def _empty(param: Param) -> Any:
    if param.type == "list":
        return []
    if param.type == "int":
        return 0
    if param.type == "float":
        return 0.0
    if param.type == "bool":
        return False
    if param.type == "enum":
        return param.choices[0] if param.choices else ""
    return ""


def _input_model(plugin: AuthoredPlugin) -> type[BaseModel]:
    """A pydantic model, so ``Tool.schema()`` and the strict-schema contract
    are untouched: the model is handed the same shape of JSON Schema it gets
    for every built-in tool, with ``additionalProperties: false``."""
    fields: dict[str, Any] = {}
    for param in plugin.params:
        kwargs: dict[str, Any] = {"description": param.description or param.name}
        if param.type in ("string", "text", "path") and param.pattern:
            kwargs["pattern"] = param.pattern
        if param.type in ("int", "float"):
            if param.minimum is not None:
                kwargs["ge"] = param.minimum
            if param.maximum is not None:
                kwargs["le"] = param.maximum
        if param.max_length is not None and param.type in ("string", "text", "path", "list"):
            kwargs["max_length"] = param.max_length
        default = ... if param.required else (
            param.default if param.default is not None else _empty(param)
        )
        fields[param.name] = (_field_type(param), Field(default, **kwargs))
    safe = re.sub(r"[^A-Za-z0-9]", "_", plugin.name).title().replace("_", "")
    return create_model(f"{safe or 'Command'}Input", **fields)


def _describe(plugin: AuthoredPlugin) -> str:
    """The description plus the prose body: the model reads both."""
    parts = [plugin.description.strip()]
    if plugin.prose.strip():
        parts.append(plugin.prose.strip())
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------
# path handling
# --------------------------------------------------------------------------

def _path_values(plugin: AuthoredPlugin, values: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for param in plugin.params:
        raw = values.get(param.name)
        if param.type == "path" and isinstance(raw, str) and raw.strip():
            out.append((param.name, raw))
        elif param.type == "list" and param.item_type == "path" and isinstance(raw, list):
            out.extend((param.name, str(item)) for item in raw if str(item).strip())
    return out


def _check_paths(plugin: AuthoredPlugin, values: dict[str, Any], root: Path) -> str:
    """"" when every path parameter stays inside the project, else the refusal."""
    for name, raw in _path_values(plugin, values):
        try:
            candidate = Path(raw).expanduser()
            resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        except (OSError, ValueError, RuntimeError):
            return (f"Error: {name}={raw!r} is not a usable path.")
        if any(_SECRET_PARTS.match(part) for part in resolved.parts):
            return (f"Error: {name}={raw!r} names a secret-bearing path. A "
                    "command tool cannot be used to reach .ssh or .env files.")
        try:
            resolved.relative_to(root)
        except ValueError:
            return (f"Error: {name}={raw!r} resolves to {resolved}, outside the "
                    f"project root {root}. Command tools are confined to the "
                    "project.")
    return ""


def _workdir(plugin: AuthoredPlugin, values: dict[str, Any], root: Path) -> Path:
    if plugin.cwd_mode == "file_dir":
        for _name, raw in _path_values(plugin, values):
            candidate = Path(raw)
            resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
            return resolved if resolved.is_dir() else resolved.parent
        return root
    if plugin.cwd_mode and plugin.cwd_mode != "project":
        target = (root / plugin.cwd_mode).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return root
        return target if target.is_dir() else root
    return root


def _child_env(plugin: AuthoredPlugin) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _BASE_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key in plugin.env_from:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(plugin.env_literal)
    return env


# --------------------------------------------------------------------------
# output mapping
# --------------------------------------------------------------------------

def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    return decode_output(raw).replace("\r\n", "\n")


def _map_output(
    plugin: AuthoredPlugin, stdout: str, stderr: str, code: int, ok: bool,
    meta: dict[str, Any],
) -> ToolResult:
    limit = plugin.max_output_chars
    note = "" if ok else f"(exit code {code})\n"

    if plugin.output == "json":
        try:
            parsed = json.loads(stdout or "null")
        except (TypeError, ValueError) as exc:
            return ToolResult(
                content=(f"Error: {plugin.name} declares output: json but its "
                         f"stdout did not parse: {exc}\n"
                         + truncate(stdout.strip(), 2000)),
                is_error=True, ui_meta=meta,
            )
        return ToolResult(content=note + truncate(_json_for_model(parsed), limit),
                          ui_meta=meta)

    if plugin.output == "lines":
        lines = [line for line in stdout.splitlines() if line.strip()]
        meta["line_count"] = len(lines)
        return ToolResult(content=note + truncate("\n".join(lines), limit) or "(no output)",
                          ui_meta=meta)

    if plugin.output == "file":
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix=f"{plugin.name}-", delete=False,
                encoding="utf-8",
            )
            with handle as fh:
                fh.write(stdout)
            meta["output_file"] = handle.name
        except OSError as exc:
            return ToolResult(content=f"Error: could not write the output file: {exc}",
                              is_error=True, ui_meta=meta)
        head = truncate(stdout, 2000, hint="full output is in the file above")
        return ToolResult(content=f"{note}Output written to {handle.name}\n\n{head}",
                          ui_meta=meta)

    body = stdout if not stderr else "\n".join(t for t in (stdout, stderr) if t)
    return ToolResult(content=note + (truncate(body.strip(), limit) or "(no output)"),
                      ui_meta=meta)


def _json_for_model(parsed: Any) -> str:
    """The cheaper of TOON and indented JSON, for a payload nobody declared.

    Every other converted site knows its own shape, so TOON always wins there.
    This one does not: an authored command with ``output: json`` can print
    anything at all. TOON is usually shorter -- it drops the braces and the
    repeated keys -- but not always: on a bare scalar, a two-field object or a
    single long quoted string the ```toon fence costs more than the encoding
    saves. Rather than guess, encode both and keep the shorter; both are
    readable, so length is the whole of the decision. Ties go to TOON, because
    it is what the rest of the tool surface speaks.
    """
    as_json = json.dumps(parsed, indent=2, ensure_ascii=False)
    as_toon = toon.fenced(parsed)
    return as_toon if len(as_toon) <= len(as_json) else as_json
