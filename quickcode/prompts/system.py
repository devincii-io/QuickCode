"""System prompt rendering — composed from sections, stable for the session.

The prompt text lives in ``prompts/sections.py``: one section per block, each
with an id, an order and a tier saying whether it may be changed. This module
is the thin composition entry point the rest of the app calls.

Everything here must be stable within a session so the cache prefix stays
byte-identical across turns. Dynamic state travels as ``<system-reminder>``
blocks inside user messages (see ``reminder`` helpers), never here.
"""

from __future__ import annotations

from quickcode.config import Environment
from quickcode.prompts.sections import PromptContext, RenderedSection, compose


def _context(
    env: Environment,
    model: str,
    provider: str,
    headless: bool,
    plan: bool,
    orchestration: bool,
    overrides: dict[str, str] | None,
) -> PromptContext:
    return PromptContext(
        env=env,
        model=model,
        provider=provider,
        headless=headless,
        plan=plan,
        orchestration=orchestration,
        overrides=overrides or {},
    )


def render_system_prompt(
    env: Environment,
    *,
    model: str = "an unknown model",
    provider: str = "the configured provider",
    headless: bool = False,
    plan: bool = False,
    orchestration: bool = False,
    overrides: dict[str, str] | None = None,
) -> str:
    """Pure function: env → the frozen system prompt for the session.

    ``model``/``provider`` name the running backend so the agent can answer
    "what are you?" accurately. Both are stable within a session, so the cache
    prefix stays byte-identical across turns (switching model mid-session
    rebuilds history anyway).

    ``overrides`` maps a section id to a replacement body; locked and
    generated sections ignore it.
    """
    text, _ = compose(
        _context(env, model, provider, headless, plan, orchestration, overrides)
    )
    return text


def render_with_sections(
    env: Environment,
    *,
    model: str = "an unknown model",
    provider: str = "the configured provider",
    headless: bool = False,
    plan: bool = False,
    orchestration: bool = False,
    overrides: dict[str, str] | None = None,
) -> tuple[str, list[RenderedSection]]:
    """The prompt plus where each section landed in it, for the UI.

    Same composition, same bytes -- this variant just keeps the offsets, so a
    reader can be shown which section produced which part of the prompt.
    """
    return compose(
        _context(env, model, provider, headless, plan, orchestration, overrides)
    )


def system_reminder(content: str) -> str:
    """Wrap dynamic state for injection into a user message."""
    return f"<system-reminder>\n{content}\n</system-reminder>"


# Short, per-turn descriptions of what the active permission mode allows. Ride
# in the user turn (never the cache-stable prefix) so the model always knows
# its live constraints even when the user cycles mode mid-session.
_MODE_REMINDERS = {
    "plan": (
        "Permission mode: PLAN. Investigate and design only — file-mutating "
        "tools are withheld. Present your plan via the plan tool; do not "
        "implement yet."
    ),
    "ask": (
        "Permission mode: ASK. You may edit files and run commands, but each "
        "mutating action asks the user for approval first. Batch related edits."
    ),
    "auto-edit": (
        "Permission mode: AUTO-EDIT. File edits within the project apply "
        "without asking; commands outside the allowlist still prompt. Keep "
        "changes tightly scoped to the request."
    ),
    "dontask": (
        "Permission mode: DONTASK. Approved actions proceed without prompting. "
        "Stay within the requested scope; avoid destructive operations."
    ),
    "yolo": (
        "Permission mode: YOLO. All actions run without prompts, including "
        "outside the project. Be careful and deliberate."
    ),
}


def mode_reminder(mode_value: str) -> str:
    """The reminder body for a permission mode value, or '' if unknown."""
    return _MODE_REMINDERS.get(mode_value, "")
