"""Agent definitions: the built-in ``explore``/``general`` shapes plus a loader
for user/project ``.md`` definitions with YAML-ish frontmatter.

A definition names a subagent's prompt, allowed tools, model role, and the
maximum permission mode it may run at (``mode_cap``). Project definitions
(``.quickcode/agents/``) shadow user ones (``~/.quickcode/agents/``) by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from quickcode.core.permissions import Mode

# The full core toolset a subagent could inherit (never the ``agent`` tool by
# default — that's gated separately by spawn depth).
ALL_TOOLS = ["read", "write", "edit", "glob", "grep", "bash", "task"]


@dataclass
class AgentDef:
    name: str
    description: str
    # Allowed tool names, or None to inherit the full core toolset.
    tools: list[str] | None = None
    # "worker" | "orchestrator" | an explicit model slug.
    model: str = "worker"
    mode_cap: Mode = Mode.ask
    max_turns: int = 30
    color: str = "cyan"
    # explore skips project instructions for speed / lean context.
    skip_project_instructions: bool = False
    prompt_body: str = ""


_EXPLORE_PROMPT = """\
You are a read-only investigation subagent. Your job is to search, read, and
analyze — never to modify anything. You have read, glob, and grep only.

- Answer the delegated task and nothing more. Do not go on tangents.
- Cite concrete evidence: file paths with line numbers (path:line).
- Your FINAL message is the entire result your spawner receives — they see none
  of your intermediate steps. Make it self-contained and structured exactly as
  the delegation's output_format asks.
- Be concise. Findings and paths, not narration."""

_GENERAL_PROMPT = """\
You are a general-purpose subagent handling a delegated, self-contained task.
You have the full toolset within a bounded scope.

- Stay strictly inside the files/directories named in the delegation's
  boundaries; those outside are owned by others.
- Your FINAL message is the entire result your spawner receives — they see none
  of your intermediate tool calls. Report what you did, the paths you touched,
  and anything the spawner must know to proceed.
- Write large outputs to files and reference the path in your report rather than
  pasting the whole thing back."""


def builtin_defs() -> dict[str, AgentDef]:
    return {
        "explore": AgentDef(
            name="explore",
            description=(
                "Read-only investigation: search the codebase/docs and report "
                "findings. The cheap, parallelizable fan-out unit — spawn several "
                "with distinct boundaries for independent questions."
            ),
            tools=["read", "glob", "grep"],
            model="worker",
            mode_cap=Mode.ask,
            skip_project_instructions=True,
            prompt_body=_EXPLORE_PROMPT,
        ),
        "general": AgentDef(
            name="general",
            description=(
                "General-purpose worker with the full toolset for a self-contained, "
                "bounded task (e.g. implement one module, run and digest a test suite)."
            ),
            tools=None,  # inherit all
            model="worker",
            mode_cap=Mode.auto_edit,
            prompt_body=_GENERAL_PROMPT,
        ),
    }


# --------------------------------------------------------------------------
# .md loader
# --------------------------------------------------------------------------

_PROJECT_DIR = Path(".quickcode") / "agents"
_USER_DIR = Path.home() / ".quickcode" / "agents"


def load_defs(cwd: Path) -> dict[str, AgentDef]:
    """Built-ins, then user defs, then project defs (each shadows the prior)."""
    defs = builtin_defs()
    for d in (_USER_DIR, cwd / _PROJECT_DIR):
        if d.is_dir():
            for md in sorted(d.glob("*.md")):
                try:
                    parsed = _parse_def(md)
                except Exception:
                    continue
                if parsed is not None:
                    defs[parsed.name] = parsed
    return defs


def _parse_def(path: Path) -> AgentDef | None:
    text = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(text)
    name = meta.get("name") or path.stem
    tools_raw = meta.get("tools")
    tools = _parse_list(tools_raw) if tools_raw else None
    if tools is not None:
        tools = [t for t in tools if t in ALL_TOOLS]
    try:
        mode_cap = Mode(meta.get("mode_cap", "ask"))
    except ValueError:
        mode_cap = Mode.ask
    try:
        max_turns = int(meta.get("max_turns", "30"))
    except (TypeError, ValueError):
        max_turns = 30
    return AgentDef(
        name=name,
        description=meta.get("description", f"Custom agent '{name}'."),
        tools=tools,
        model=meta.get("model", "worker"),
        mode_cap=mode_cap,
        max_turns=max_turns,
        color=meta.get("color", "cyan"),
        prompt_body=body.strip(),
    )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal ``---`` frontmatter parser (key: value; no nested YAML)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    body_start = len(lines)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            body_start = i + 1
            break
        if ":" in lines[i]:
            key, _, val = lines[i].partition(":")
            meta[key.strip()] = val.strip()
    return meta, "\n".join(lines[body_start:])


def _parse_list(raw: str) -> list[str]:
    """Parse ``[read, glob, grep]`` or ``read, glob`` into a list."""
    raw = raw.strip().lstrip("[").rstrip("]")
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]
