"""``GET /api/prompt``: the orchestrator's system prompt, live or frozen.

Two different questions, and the payload says which one it answers.

**Live** (no ``conv``) is "what will the next session be told". It resolves the
active composition the way ``ConversationManager.open()`` does and renders it
through ``prompts/system.render_with_sections`` -- so a composition that
rewrites a section, or one that cannot spawn and therefore gets no delegation
playbook, is reflected here rather than only in the session it opens.

**Frozen** (``?conv=<id>``) is "what is this session being told". The answer
is the text in that conversation's history, which is the only place those
bytes exist: the session composed them at open from a snapshot, and anything
on disk may have moved since. Section boundaries are recovered by rendering
the session's own inputs again; when that reproduces the text exactly the
offsets are exact, and when it does not (an authored section was added or
removed since the session opened) each section is located by its text and the
payload says ``exact: false`` rather than drawing a boundary it cannot vouch
for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from quickcode.core.permissions import Mode
from quickcode.kernel import preset as preset_module
from quickcode.kernel.composition import ORCHESTRATOR_ID
from quickcode.kernel.resolve import resolve_composition, runtime_limits, session_pool
from quickcode.prompts.system import render_with_sections
from quickcode.server.http import DEFAULT, PROJECT, scoped
from quickcode.subagents.definitions import load_defs


def _section_json(section: Any, start: int | None = None,
                  end: int | None = None) -> dict[str, Any]:
    return {
        "id": section.id, "title": section.title, "tier": section.tier,
        "start": section.start if start is None else start,
        "end": section.end if end is None else end,
    }


def _live(manager: Any) -> dict[str, Any]:
    cwd = Path(manager.cwd)
    preset = preset_module.resolve(cwd)
    limits = runtime_limits(cwd)
    resolved = resolve_composition(
        ORCHESTRATOR_ID,
        pool=session_pool(cwd, list(manager.registry_factory().tools.values())),
        preset=preset,
        defs=load_defs(cwd),
        cwd=cwd,
        max_depth=limits.max_depth,
        resolve_model=manager.resolve_role,
    )
    model = manager.config.last_model or manager.config.profile.resolve("orchestrator")
    text, sections = render_with_sections(
        manager.env,
        model=model,
        provider=manager.provider_name,
        orchestration=bool(resolved.spawns),
        overrides=dict(resolved.section_bodies),
    )
    return {
        "text": text,
        "sections": [_section_json(s) for s in sections],
        "frozen": False,
        "exact": True,
        "preset": preset.id,
        "model": model,
        "digest": resolved.digest(),
    }


def _locate(text: str, sections: list[Any]) -> list[dict[str, Any]]:
    """Each section found by its own text, in order, never overlapping."""
    out: list[dict[str, Any]] = []
    cursor = 0
    for section in sections:
        at = text.find(section.text, cursor) if section.text else -1
        if at < 0:
            continue
        out.append(_section_json(section, at, at + len(section.text)))
        cursor = at + len(section.text)
    return out


def _frozen(manager: Any, conv: Any) -> dict[str, Any]:
    text = conv.agent.history.system_prompt
    resolved = conv.resolved
    model = conv.agent.model
    in_plan = conv.agent.mode == Mode.plan

    # The prompt is rendered at open and re-rendered by a model or composition
    # switch, each time with the plan block decided by the mode *then*. Trying
    # both is cheaper than recording which, and only an exact match counts.
    rendered: list[Any] = []
    for plan in (in_plan, not in_plan):
        candidate, rendered = render_with_sections(
            manager.env,
            model=model,
            provider=manager.provider_name,
            plan=plan,
            orchestration=bool(resolved.spawns),
            overrides=dict(resolved.section_bodies),
        )
        if candidate == text:
            sections, exact = [_section_json(s) for s in rendered], True
            break
    else:
        sections, exact = _locate(text, rendered), False

    return {
        "text": text,
        "sections": sections,
        "frozen": True,
        "exact": exact,
        "preset": conv.preset_id,
        "model": model,
        "digest": resolved.digest(),
        "conv": conv.conv_id,
    }


def prompt_payload(manager: Any, conv_id: str = "") -> dict[str, Any]:
    if not conv_id:
        return _live(manager)
    conv = manager.get(conv_id)
    if conv is None:
        raise HTTPException(404, f"no live conversation {conv_id!r}")
    return _frozen(manager, conv)


def prompt(manager: Any, conv: str = "") -> dict:
    return prompt_payload(manager, conv)


def register_prompt_routes(app: FastAPI, hub: Any) -> None:
    scoped(app, hub, "GET", "/prompt", prompt, shapes=(PROJECT, DEFAULT))
