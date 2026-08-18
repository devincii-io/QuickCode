"""Agent definitions: an *identity* owning exactly one ``Composition``.

An ``AgentDef`` is a name, a description, a role and a composition. Everything
that used to be a scalar on this class -- ``tools``, ``model``, ``mode_cap``,
``max_turns`` and the rest -- is now a read-through property over
``composition``, so ``manifest.agent_specs()``, the settings UI and every
current caller keep working unchanged while there is exactly one place a
capability can be written down.

``role`` is the discriminator, and it gates exactly four things:

===================  ===================================  ====================
Aspect               ``orchestrator``                     ``subagent``
===================  ===================================  ====================
``plan`` tool        eligible                             never
permission callback  the interactive round-trip           auto-deny
``ceiling``          a cap on the live-adjustable mode    a hard cap at spawn
``max_turns``        not applicable                       the delegation budget
===================  ===================================  ====================

The id ``@orchestrator`` is reserved. The leading ``@`` is why no ordinary
agent name can collide with it, and ``load_defs`` refuses ``role:
orchestrator`` on anything else.

Project definitions (``.quickcode/agents/``) shadow user ones
(``~/.quickcode/agents/``) by name.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from quickcode.core.permissions import Mode
from quickcode.kernel.composition import ORCHESTRATOR_ID, Composition, parse_mode

log = logging.getLogger("quickcode.subagents.definitions")

Role = Literal["orchestrator", "subagent"]


class _Unset:
    """Distinguishes "said nothing" from "said null"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()

# A definition's ``tools:`` list is resolved against the live tool pool at
# spawn time, so there is no fixed vocabulary here. ``tools: null`` inherits
# what the spawner holds (the session pool at depth 0), never more.


class AgentDef:
    """One agent's identity plus its composition.

    The constructor still takes every legacy scalar as a keyword so existing
    call sites -- and every ``.md`` file on disk -- build the same object they
    always did; they are folded into a ``Composition`` on the way in.
    """

    __slots__ = ("name", "description", "role", "composition", "source", "path",
                 "prompt_body")

    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        role: Role = "subagent",
        composition: Composition | None = None,
        source: str = "internal",
        path: str = "",
        prompt_body: str = "",
        # legacy scalars, folded into the composition
        tools: list[str] | None | Any = _UNSET,
        model: str | None = None,
        models: list[str] | None = None,
        model_selectable: bool | None = None,
        mode_cap: Mode | None = None,
        max_turns: int | None = None,
        color: str | None = None,
        skip_project_instructions: bool | None = None,
        spawns: list[str] | None = None,
        sections: list[str] | None = None,
        base: str = "",
    ) -> None:
        self.name = name
        self.description = description
        self.role = role
        self.source = source
        self.path = path
        self.prompt_body = prompt_body

        comp = composition if composition is not None else Composition()
        stated: dict[str, Any] = {}
        # ``tools=None`` means "inherit what the spawner holds", which is a
        # statement and not a silence -- so it is recorded as one. Omitting the
        # argument entirely is the silence.
        if tools is not _UNSET:
            stated["tools"] = None if tools is None else tuple(tools)
        if spawns is not None:
            stated["spawns"] = tuple(spawns)
        if sections is not None:
            stated["sections"] = tuple(sections)
        if model is not None:
            stated["model"] = model
        if models is not None:
            stated["models"] = tuple(models)
        if model_selectable is not None:
            stated["model_selectable"] = bool(model_selectable)
        if mode_cap is not None:
            stated["ceiling"] = parse_mode(mode_cap)
        if max_turns is not None:
            stated["max_turns"] = int(max_turns)
        if color is not None:
            stated["color"] = color
        if skip_project_instructions is not None:
            stated["skip_project_instructions"] = bool(skip_project_instructions)
        if base:
            stated["base"] = base
        self.composition = comp.with_fields(**stated) if stated else comp

    # -- read-through properties -----------------------------------------

    @property
    def tools(self) -> list[str] | None:
        patterns = self.composition.tools
        return None if patterns is None else list(patterns)

    @property
    def spawns(self) -> list[str] | None:
        patterns = self.composition.spawns
        return None if patterns is None else list(patterns)

    @property
    def model(self) -> str:
        return self.composition.model or "worker"

    @property
    def models(self) -> list[str]:
        return list(self.composition.models)

    @property
    def model_selectable(self) -> bool:
        return self.composition.model_selectable

    @property
    def mode_cap(self) -> Mode:
        return self.composition.ceiling

    @property
    def max_turns(self) -> int:
        return self.composition.max_turns

    @property
    def color(self) -> str:
        return self.composition.color

    @property
    def skip_project_instructions(self) -> bool:
        return self.composition.skip_project_instructions

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AgentDef({self.name!r}, role={self.role!r})"


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
            tools=None,  # inherit what the spawner holds
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
    """Built-ins, then user defs, then project defs (each shadows the prior).

    Snapshot this once per session: resolving it live on every spawn would
    change an agent's behaviour mid-conversation, which is the same lie the
    frozen preset exists to prevent, and worse for subagents because the parent
    was already told the agent roster in the ``agent`` tool's schema.
    """
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
    # Kept verbatim: patterns are resolved against the live tool pool at spawn
    # time (tools/registry.py:select), so a definition may name a plugin or an
    # MCP tool -- or a glob like ``mcp__*`` -- that this file cannot know about.
    tools = _parse_list(tools_raw) if tools_raw else None
    try:
        mode_cap = Mode(meta.get("mode_cap", "ask"))
    except ValueError:
        mode_cap = Mode.ask
    try:
        max_turns = int(meta.get("max_turns", "30"))
    except (TypeError, ValueError):
        max_turns = 30
    models_raw = meta.get("models")
    models = _parse_list(models_raw) if models_raw else []
    selectable = meta.get("model_selectable", "true").strip().lower() not in (
        "false", "no", "0"
    )

    role: Role = "subagent"
    declared = meta.get("role", "").strip().lower()
    if declared == "orchestrator":
        if name == ORCHESTRATOR_ID:
            role = "orchestrator"
        else:
            # Provenance is the one thing a definition cannot be trusted to say
            # about itself: the reserved id is what makes an agent the
            # orchestrator, not a frontmatter claim.
            log.warning(
                "%s declares role: orchestrator but is not %s; loading as a subagent",
                path, ORCHESTRATOR_ID,
            )

    spawns_raw = meta.get("spawns")
    sections_raw = meta.get("sections")
    return AgentDef(
        name=name,
        description=meta.get("description", f"Custom agent '{name}'."),
        role=role,
        source="config",
        path=str(path),
        tools=tools,
        spawns=_parse_list(spawns_raw) if spawns_raw else None,
        sections=_parse_list(sections_raw) if sections_raw else None,
        base=meta.get("base", ""),
        model=meta.get("model", models[0] if models else "worker"),
        models=models,
        model_selectable=selectable,
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
