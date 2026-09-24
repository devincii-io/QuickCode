"""Command hooks as plugin specs, so Settings lists the hooks the loop runs.

One spec per configured command, kind ``hook``, source ``config`` -- the same
shape an MCP server from settings.json takes. The enable switch is real: a
disabled hook's id is what ``load_hooks`` leaves out.

Refused project hooks (untrusted project) are not listed as plugins. A card with
a switch implies something that could run; they are reported as a problem on
the page instead, and in the trust review.
"""

from __future__ import annotations

import json
from pathlib import Path

from quickcode.hooks.config import TOOL_EVENTS, HookCommand, HookConfig
from quickcode.kernel.spec import Effect, PluginSpec, PluginView

# WHAT, per event. Each <= 90 characters (kernel/spec.py).
_SUMMARY = {
    "PreToolUse": "Runs your command before a matching tool call; it can refuse it or "
                  "demand a prompt.",
    "PostToolUse": "Runs your command after a matching tool call; it can send the model "
                   "a note.",
    "UserPromptSubmit": "Runs your command on each message you send; it can refuse it or "
                        "add context.",
    "Stop": "Runs your command each time a turn finishes on its own.",
    "SessionStart": "Runs your command once, as a session's first turn starts; it can "
                    "add context.",
}

_AFFECTS: dict[str, tuple[Effect, ...]] = {
    "PreToolUse": ("permissions", "loop"),
    "PostToolUse": ("loop",),
    "UserPromptSubmit": ("loop",),
    "Stop": ("loop",),
    "SessionStart": ("loop",),
}


def _short(command: str, limit: int = 60) -> str:
    lines = command.splitlines() or [""]
    more = len(lines) > 1 or len(lines[0]) > limit
    return lines[0][:limit] + (" …" if more else "")


def _block(hook: HookCommand) -> str:
    group: dict[str, object] = {}
    if hook.matcher:
        group["matcher"] = hook.matcher
    group["hooks"] = [{"type": "command", "command": hook.command,
                       "timeout": hook.timeout_s}]
    return json.dumps({"hooks": {hook.event: [group]}}, indent=2, ensure_ascii=False)


def _spec(hook: HookCommand, *, disabled: bool) -> PluginSpec:
    tool_event = hook.event in TOOL_EVENTS
    where = Path(hook.source).name if hook.source else "settings.json"
    scope = "your user settings" if hook.scope == "user" else "this project's settings"
    consequence = (
        f"Switched off, the command stops running for {hook.event}; it stays in "
        f"{scope} ({where}), and is edited on the Hooks page or in that file."
    )
    if hook.scope == "project":
        consequence += " It runs only while this project is trusted."
    if hook.event == "PreToolUse":
        consequence += (" It can make a call stricter and never looser: an allow from "
                        "it skips no prompt.")
    title = f"{hook.event}" + (f" [{hook.matcher}]" if tool_event and hook.matcher else "")
    return PluginSpec(
        id=hook.id,
        kind="hook",
        title=f"{title}: {_short(hook.command)}",
        description=hook.command,
        group="Hooks",
        source="config",
        enabled_by_default=True,
        summary=_SUMMARY[hook.event],
        affects=_AFFECTS[hook.event],
        audience="all_agents" if tool_event else "orchestrator",
        consequence=consequence,
        docs_anchor="docs/HOOKS.md#events",
        metadata={
            "event": hook.event, "matcher": hook.matcher, "command": hook.command,
            "timeout_s": hook.timeout_s, "scope": hook.scope, "source": hook.source,
            "disabled": disabled,
        },
        view=lambda: PluginView(format="json", content=_block(hook),
                                title=f"{hook.event} hook in {where}"),
    )


def hook_specs(config: HookConfig) -> list[PluginSpec]:
    return [
        *(_spec(h, disabled=False) for h in config.hooks),
        *(_spec(h, disabled=True) for h in config.disabled),
    ]
