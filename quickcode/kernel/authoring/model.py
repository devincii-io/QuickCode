"""``AuthoredPlugin``: one parsed file, plus the converters into live objects.

The file is the truth; this is the shape it takes once it has been read and
validated. Nothing here touches the filesystem and nothing here validates --
by the time an ``AuthoredPlugin`` exists, ``schema.validate`` has already said
it is loadable.

The converters are deliberately thin. ``to_tool()`` builds a ``CommandTool``,
``to_agent_def()`` builds the same ``AgentDef`` the ``.quickcode/agents/``
loader builds, ``to_prompt_section()`` builds a ``PromptSection`` that
``sections.compose()`` cannot tell from an internal one. No new execution path
is invented anywhere: every kind here is data the runtime already consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Scope = str  # "user" | "project"

# The parameter types a command tool may declare. ``path`` is the one with
# behaviour of its own (see CommandTool); the rest are shapes.
PARAM_TYPES = ("string", "text", "int", "float", "bool", "enum", "path", "list")

# What `output:` may say.
OUTPUT_MODES = ("text", "json", "lines", "file")

CWD_MODES = ("project", "file_dir")

WHEN_VALUES = ("always", "plan", "orchestration", "headless")


@dataclass(frozen=True)
class Param:
    """One typed parameter the model fills in."""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()
    item_type: str = "string"
    pattern: str = ""
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    # bool only: the token emitted when the value is true. Defaults to --<name>.
    flag: str = ""


@dataclass(frozen=True)
class AuthoredPlugin:
    """One `.quickcode/plugins/*.md` file, parsed and accepted."""

    kind: str
    name: str
    scope: Scope
    path: str = ""
    title: str = ""
    description: str = ""
    group: str = ""
    enabled_by_default: bool = True
    derived_from: str = ""
    source_text: str = ""
    # The free-text payload: a tool's long description, an agent's system
    # prompt, a section's body.
    prose: str = ""

    # -- kind: tool -------------------------------------------------------
    params: tuple[Param, ...] = ()
    argv: tuple[str, ...] = ()
    label: str = ""
    cwd_mode: str = "project"
    timeout_ms: int = 120_000
    output: str = "text"
    max_output_chars: int = 30_000
    success_exit_codes: tuple[int, ...] = (0,)
    on_nonzero: str = "error"
    # The author's claim, recorded and never honoured -- see CommandTool.
    read_only_declared: bool = False
    permission_target: str = ""
    env_from: tuple[str, ...] = ()
    env_literal: dict[str, str] = field(default_factory=dict)
    stdin: str = ""

    # -- kind: agent ------------------------------------------------------
    agent_meta: dict[str, str] = field(default_factory=dict)

    # -- kind: prompt -----------------------------------------------------
    order: int = 200
    after: str = ""
    applies_to: tuple[str, ...] = ("main",)
    when: str = "always"

    @property
    def id(self) -> str:
        return f"{self.kind}.{self.name}"

    @property
    def display_title(self) -> str:
        return self.title or self.name

    def params_by_name(self) -> dict[str, Param]:
        return {p.name: p for p in self.params}

    # -- converters -------------------------------------------------------

    def to_tool(self):
        """A live ``CommandTool``. Imported lazily: building a registry must
        not drag pydantic model construction into plugin discovery."""
        from quickcode.tools.command import CommandTool

        return CommandTool(self)

    def to_agent_def(self):
        from quickcode.subagents.definitions import agent_def_from_meta

        return agent_def_from_meta(
            self.agent_meta, self.prose, path=self.path, source="authored",
            fallback_name=self.name,
        )

    def to_prompt_section(self):
        """A ``PromptSection`` indistinguishable from an internal one.

        ``when`` is folded into the render function rather than into
        ``compose()``: a section that should not appear renders empty, and
        ``compose()`` already drops empty sections. That keeps the byte-
        stability guarantee -- the join and the empty-section drop -- closed.
        """
        from quickcode.prompts.sections import PromptContext, PromptSection

        body = self.prose.strip()
        when = self.when

        def render(ctx: PromptContext) -> str:
            if when == "plan" and not ctx.plan:
                return ""
            if when == "orchestration" and not ctx.orchestration:
                return ""
            if when == "headless" and not ctx.headless:
                return ""
            return body

        return PromptSection(
            id=self.id,
            title=self.display_title,
            order=self.order,
            tier="free",
            render=render,
            description=self.description or "An authored prompt section.",
        )
