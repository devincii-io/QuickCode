"""One plugin per system-prompt section."""

from __future__ import annotations

from quickcode.kernel.manifest._text import lazy_view
from quickcode.kernel.spec import Audience, PluginSpec, Recourse, SettingSpec

# Every section reaches the orchestrator's system prompt and nothing else:
# subagents are rendered from the separate template in ``prompts/subagent.py``
# and never see these blocks. That is a fact about today's wiring, so it is
# stated once here rather than repeated per row.
_SECTION_AUDIENCE: Audience = "orchestrator"

# A replacement body is returned verbatim by ``PromptSection.body``: it is not
# formatted and the render function's own conditions are not consulted. For the
# four sections that render conditionally, that is a real surprise, so each one
# says so.
_OVERRIDE_UNCONDITIONAL = (
    "A replacement body is used unconditionally, so text written here appears "
    "even in the sessions where the default renders nothing."
)

# id -> (summary, consequence, effect_detail on the body setting)
_SECTION_PROSE: dict[str, tuple[str, str, str]] = {
    "prompt.identity": (
        "Tells the agent it is QuickCode and which model is answering right now.",
        "This is the line the agent answers identity questions from, including "
        "after a model switch mid-conversation. Rewriting it changes what it "
        "says it is, not what it can do.",
        "Rendered with the live model and provider names. A replacement body is "
        "used verbatim, so placeholders written into your own text are not "
        "substituted.",
    ),
    "prompt.tone": (
        "How replies read: length, no preamble, no narrating routine tool calls.",
        "Governs the prose in the chat pane and nothing else. Loosening it costs "
        "reading time, not correctness.",
        "Instruction text only. It says nothing about which tools are used or "
        "when, so a change here cannot alter what the agent does.",
    ),
    "prompt.autonomy": (
        "How far the agent goes on its own before it stops to ask you.",
        "Widening it means fewer questions and more unasked-for work; narrowing "
        "it means the agent stops for confirmations it could have inferred.",
        "Instruction text only. The permission engine decides what is actually "
        "allowed, so this changes what the agent attempts, not what it can do.",
    ),
    "prompt.conventions": (
        "Codebase manners: match local style, check imports, no unasked comments.",
        "Drop it and the agent is no longer told to read neighbouring files "
        "first, so new code matches its own habits rather than the project's.",
        "Instruction text only. Nothing here is enforced by a tool; it is read "
        "once per session along with the rest of the prompt.",
    ),
    "prompt.task_management": (
        "When work goes on the task board instead of being improvised in place.",
        "Remove it and the task tools stay available but go mostly unused, so "
        "multi-step work leaves no visible plan behind.",
        "Only meaningful while the task tools are in the agent's tool list. The "
        "text names them; it does not grant them.",
    ),
    "prompt.tool_use_policy": (
        "The rules for choosing, batching and paginating tool calls.",
        "It names this machine's shell and platform, so the commands the agent "
        "writes are the ones the local shell will actually accept.",
        "Rendered with this machine's shell and platform. Batching, pagination "
        "and read-before-edit are the half of the tool contract the model holds "
        "up.",
    ),
    "prompt.verification": (
        "Tells the agent to run the project's checks before calling work done.",
        "Without it the agent can still run tests but is no longer asked to, so "
        "\"done\" starts to mean \"written\".",
        "Instruction text only. It does not run anything and does not know which "
        "test command this project uses.",
    ),
    "prompt.result_format": (
        "One worked example of the TOON encoding structured tool results arrive in.",
        "It is how the agent knows a row count it can check against what it "
        "actually read, so a truncated search stops looking like an exhaustive "
        "one.",
        "Four lines showing a header, a count and two rows. It describes what "
        "the tools already emit; it does not decide the encoding.",
    ),
    "prompt.environment": (
        "The session's facts: directory, platform, shell, date and git branch.",
        "Read from the machine when the session opens. It is what the agent "
        "knows about where it is running without spending a tool call.",
        "Generated from the live environment at session open; there is no "
        "template text behind it to edit.",
    ),
    "prompt.project_instructions": (
        "Your project's own QUICKCODE.md, AGENTS.md or CLAUDE.md, quoted in full.",
        "This is the one part of the prompt you change by editing a file in the "
        "repository rather than a setting in here.",
        "Filled from the first instructions file found in the project, with the "
        "source path recorded in the block's opening tag.",
    ),
    "prompt.orchestration": (
        "The delegation playbook: when to spawn, how many, and how to word a task.",
        "Present only when the session has the delegation tools. Take those away "
        "and the section renders empty and is dropped from the prompt entirely.",
        "Rendered only when the session can spawn subagents. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.send_message_hint": (
        "Reminds the agent a finished subagent can be resumed instead of respawned.",
        "Without it the agent tends to spawn a fresh subagent for a follow-up, "
        "losing the first one's context and paying for the same work twice.",
        "Rendered only when the session can spawn subagents. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.plan_mode": (
        "Added when a session opens in plan mode: investigate, design, change nothing.",
        "Present when a session opens in plan mode and kept for the session, "
        "since the prompt is frozen; a mode switch reaches the model as a "
        "reminder. The mutating tools are withheld by the plan-mode hook "
        "whether or not this text is present.",
        "Rendered only while the mode is plan. " + _OVERRIDE_UNCONDITIONAL,
    ),
    "prompt.headless": (
        "Added for non-interactive runs: never ask, the last message is the output.",
        "Present only in headless runs, where there is nobody to answer a "
        "question and an unanswered prompt would hang the process.",
        "Rendered only for headless runs. " + _OVERRIDE_UNCONDITIONAL,
    ),
}

# Why a section's body cannot be replaced, for the ones that cannot.
_SECTION_LOCKED: dict[str, tuple[str, Recourse]] = {
    "prompt.tool_use_policy": (
        "Batching, pagination and read-before-edit are the half of the tool "
        "contract the model holds up. Rewriting them locally produces calls that "
        "read correctly in the transcript and behave differently in the loop.",
        Recourse("author", "Write your own section to run after this one",
                 "prompt_section"),
    ),
    "prompt.result_format": (
        "This block describes what the tools actually encode. Rewriting it to "
        "describe a different format would not change a single tool result — "
        "it would only teach the agent to read them wrongly.",
        Recourse("docs", "The encoder is the source of truth for this block",
                 "docs/PROMPTS.md#1-system-prompt-template"),
    ),
    "prompt.environment": (
        "These are observations, not instructions. A hand-edited environment "
        "block would tell the agent it is on a platform it is not, and every "
        "shell command written after that would use the wrong syntax.",
        Recourse("settings", "Open the project elsewhere to change these facts",
                 "prompt.system"),
    ),
    "prompt.project_instructions": (
        "The body is read from the project's instructions file when the session "
        "opens. Editing the copy shown here would create a second source of "
        "truth that the next session silently overwrites.",
        Recourse("docs", "Edit the project's instructions file instead",
                 "docs/PROMPTS.md#1-system-prompt-template"),
    ),
}

_GENERIC_SECTION_RECOURSE = Recourse(
    "author", "Write your own section to run after this one", "prompt_section"
)


def prompt_section_specs(bodies: dict[str, str] | None = None) -> list[PluginSpec]:
    """One plugin per system-prompt section.

    ``bodies`` carries each section's rendered text for this project, so the
    UI shows what the agent is actually being told rather than a template.
    Sections that render conditionally (plan mode, headless) are listed even
    when inactive -- being able to read them is the point.
    """
    from quickcode.prompts import sections as prompt_sections

    rendered = bodies or {}
    out: list[PluginSpec] = []
    for section in prompt_sections.ordered():
        body = rendered.get(section.id, "")
        editable = section.tier != "locked" and not section.generated
        summary, consequence, effect_detail = _SECTION_PROSE.get(
            section.id, (section.description, "", "")
        )
        locked_because, recourse = _SECTION_LOCKED.get(
            section.id, ("", _GENERIC_SECTION_RECOURSE)
        )
        # The setting exists either way, locked when the section cannot be
        # rewritten: "you may not change this" is a far better answer than
        # "no such setting" for something the reader can plainly see.
        settings = (
            SettingSpec(
                key="body", type="text", default=body,
                tier=section.tier if editable else "locked",
                title="Section text",
                help=section.description,
                risk="This text is part of every turn's instructions. Rewriting "
                     "it changes how the agent behaves for the whole session.",
                affects=("prompt",),
                effect_detail=effect_detail,
                locked_because=locked_because,
                recourse=recourse if not editable else None,
            ),
        )
        out.append(PluginSpec(
            id=section.id,
            kind="prompt_section",
            title=section.title,
            description=section.description,
            group="Prompt",
            required=not editable,
            settings=settings,
            # A section whose body cannot be replaced reads as locked, whatever
            # its declared tier: the badge has to match the only knob it has.
            tier_hint=section.tier if editable else "locked",
            summary=summary,
            affects=("prompt",),
            audience=_SECTION_AUDIENCE,
            consequence=consequence,
            locked_because=locked_because,
            recourse=recourse if not editable else None,
            docs_anchor="docs/PROMPTS.md#1-system-prompt-template",
            metadata={
                "order": section.order,
                "generated": section.generated,
                "active": bool(body),
                "editable": editable,
                "edit_hint": (
                    "Generated from this session's facts and the project's "
                    "instructions file — edit those, not this."
                    if section.generated else ""
                ),
            },
            view=lazy_view("text", body or "(not part of this session's prompt)",
                           section.title),
        ))
    return out
