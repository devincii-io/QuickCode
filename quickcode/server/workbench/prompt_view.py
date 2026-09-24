"""The prompt half of an agent's view: its composed prompt, block by block.

Both renderers call the code the runner renders with -- ``compose()`` through
``prompts/system.render_with_sections`` for the orchestrator,
``prompts/subagent.render_subagent_prompt`` for a child -- and then say which
blocks are present, where they sit in the text, which layer wrote them, and why
each missing one is missing.
"""

from __future__ import annotations

from typing import Any

from quickcode.kernel.composition import Resolved
from quickcode.prompts import sections as sections_module
from quickcode.prompts.subagent import render_subagent_prompt
from quickcode.prompts.system import render_with_sections
from quickcode.server.manager import ConversationManager
from quickcode.server.workbench.provenance import last_prov, prov_json
from quickcode.subagents.definitions import AgentDef

# Why an internal section can be absent from a composed prompt. ``compose()``
# drops an empty section, and an empty region is invisible: without this table a
# user who set ``skip_project_instructions`` has no way to discover that the
# flag is what removed the block.
_ABSENCE_REASONS: dict[str, str] = {
    "prompt.project_instructions":
        "no QUICKCODE.md / AGENTS.md / CLAUDE.md was found in this project, so "
        "there is nothing to include",
    "prompt.orchestration":
        "this agent has no spawnable agents, so the delegation playbook is not "
        "sent",
    "prompt.send_message_hint":
        "this agent has no spawnable agents, so there is nothing to resume",
    "prompt.plan_mode":
        "this session is not in plan mode",
    "prompt.headless":
        "this is an interactive session, not a headless run",
}

_SUBAGENT_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("identity", "Identity", "generated from the agent's name and model"),
    ("role", "Instructions", "the agent's own body — this is what you edit"),
    ("environment", "Environment", "generated from the session's environment"),
    ("project_instructions", "Project instructions",
     "the project's QUICKCODE.md / AGENTS.md / CLAUDE.md"),
)


def _tag_span(text: str, tag: str) -> tuple[int, int] | None:
    open_at = text.find(f"<{tag}")
    if open_at < 0:
        return None
    close = text.find(f"</{tag}>", open_at)
    if close < 0:
        return None
    return open_at, close + len(f"</{tag}>")


def orchestrator_prompt(
    manager: ConversationManager, resolved: Resolved, *, model: str, plan: bool,
) -> dict[str, Any]:
    """The orchestrator's composed prompt, rendered by ``compose()`` itself."""
    text, rendered = render_with_sections(
        manager.env,
        model=model,
        provider=manager.provider_name,
        headless=False,
        plan=plan,
        orchestration=bool(resolved.spawns),
        overrides=dict(resolved.section_bodies),
    )
    present = {section.id for section in rendered}
    blocks = [
        {
            "id": section.id,
            "title": section.title,
            "tier": section.tier,
            "start": section.start,
            "end": section.end,
            "overridden": section.id in resolved.section_bodies,
            "provenance": prov_json(
                last_prov(resolved, f"section_bodies.{section.id}")
            ),
        }
        for section in rendered
    ]
    absences = [
        {
            "id": section.id,
            "title": section.title,
            "reason": _ABSENCE_REASONS.get(
                section.id,
                "its body resolved empty, and empty sections are dropped from "
                "the composition",
            ),
        }
        for section in sections_module.ordered()
        if section.id not in present
    ]
    return {"text": text, "blocks": blocks, "absences": absences,
            "template": "prompts/sections.py"}


def subagent_prompt(
    manager: ConversationManager, defn: AgentDef, *, model: str,
) -> dict[str, Any]:
    """A subagent's prompt, rendered by ``render_subagent_prompt`` itself.

    A subagent's prompt is a different template, not a narrowed version of the
    orchestrator's, and that fact is the single most surprising thing about
    prompt sections: editing ``prompt.tone`` does nothing here. It is stated as
    an absence rather than left to be discovered.
    """
    text = render_subagent_prompt(defn, manager.env, model=model)
    blocks: list[dict[str, Any]] = []
    for tag, title, note in _SUBAGENT_BLOCKS:
        span = _tag_span(text, tag)
        if span is None:
            continue
        blocks.append({
            "id": f"subagent.{tag}", "title": title, "tier": "free",
            "start": span[0], "end": span[1], "overridden": tag == "role",
            "note": note,
            "provenance": {
                "layer": "agent" if tag == "role" else "default",
                "source": defn.path or "prompts/subagent.py",
                "rule": "body" if tag == "role" else tag,
            },
        })

    absences: list[dict[str, Any]] = [{
        "id": "prompt.*",
        "title": "Every prompt section",
        "reason": (
            "a subagent's prompt is composed from prompts/subagent.py, not from "
            "the section list — none of the orchestrator's prompt sections "
            "reaches this agent"
        ),
    }]
    if defn.skip_project_instructions:
        absences.append({
            "id": "prompt.project_instructions",
            "title": "Project instructions",
            "reason": "omitted — this agent sets skip_project_instructions",
        })
    elif not manager.env.project_instructions.strip():
        absences.append({
            "id": "prompt.project_instructions",
            "title": "Project instructions",
            "reason": "no QUICKCODE.md / AGENTS.md / CLAUDE.md was found in this "
                      "project",
        })
    return {"text": text, "blocks": blocks, "absences": absences,
            "template": "prompts/subagent.py"}
