"""One plugin per authored file under ``.quickcode/plugins/``."""

from __future__ import annotations

from typing import Any

from quickcode.kernel.manifest._text import (
    READ_ONLY_LOCKED_BECAUSE,
    READ_ONLY_RECOURSE,
    lazy_view,
    schema_text,
)
from quickcode.kernel.spec import PluginSpec, Recourse, SettingSpec

# Why an authored plugin has no locked settings and nothing required: the tier
# system protects QuickCode's internals from you, and it does not protect your
# own files from you. Tier is a property of a plugin's *source*, not of its
# content -- a duplicated copy of a locked agent is free down to the byte.

_AUTHORED_TOOL_LOCKED = (
    "A command tool is executed as an argv array, never through a shell, so a "
    "parameter value can never become two arguments. That shape is what makes "
    "the tool safe to hand a model, and it is not a knob."
)

_AUTHORED_TOOL_RECOURSE = Recourse(
    "author", "Edit the file to change the command", "tool",
)

_READ_ONLY_CLAIM_HELP = (
    "Recorded from the file and honoured by nothing. QuickCode cannot check "
    "what a program does, so a declaration here never removes the permission "
    "prompt -- a file that could opt itself out of being asked about would be "
    "the same hole as an unaudited external server. To stop being asked, add a "
    "permission rule naming this tool."
)


def _authored_tool_spec(plugin: Any, tool: Any) -> PluginSpec:
    payload, signature = schema_text(tool)

    argv = " ".join(plugin.argv)
    scope = "this project" if plugin.scope == "project" else "every project"
    return PluginSpec(
        id=plugin.id,
        kind="tool",
        title=plugin.name,
        description=plugin.description,
        group=plugin.group or "Yours",
        source="authored",
        path=plugin.path,
        derived_from=plugin.derived_from,
        enabled_by_default=plugin.enabled_by_default,
        summary=f"Runs {plugin.argv[0] if plugin.argv else 'a command'}; "
                f"yours, from a file."[:90],
        affects=("tool_list", "permissions"),
        audience="all_agents",
        consequence=(
            f"Available in {scope}. It runs `{argv}` as an argument array with "
            "no shell involved, so a parameter value is one argument whatever "
            "it contains. Every call goes through the permission gate like any "
            "other mutating tool."
        ),
        locked_because=_AUTHORED_TOOL_LOCKED,
        recourse=_AUTHORED_TOOL_RECOURSE,
        docs_anchor="docs/design/AUTHORING.md",
        settings=(
            SettingSpec(
                key="read_only", type="bool", default=False, tier="locked",
                title="Read-only", help=_READ_ONLY_CLAIM_HELP,
                affects=("permissions",),
                effect_detail="A command tool always declares itself mutating, "
                              "so it is withheld in plan mode and prompted for "
                              "in ask mode.",
                locked_because=READ_ONLY_LOCKED_BECAUSE,
                recourse=READ_ONLY_RECOURSE,
                fact=True,
            ),
        ),
        metadata={
            "tool_name": plugin.name,
            "authored": True,
            "scope": plugin.scope,
            "argv": list(plugin.argv),
            "read_only_declared": plugin.read_only_declared,
            "read_only": False,
            "timeout_ms": plugin.timeout_ms,
            "output": plugin.output,
            "params": [p.name for p in plugin.params],
            "signature": signature,
        },
        view=lazy_view("json", payload, f"{plugin.name} schema", plugin.path),
    )


def _authored_prompt_spec(plugin: Any) -> PluginSpec:
    scope = "this project" if plugin.scope == "project" else "every project"
    when = plugin.when
    condition = {
        "always": "in every session",
        "plan": "only while the session is in plan mode",
        "orchestration": "only when the session can spawn subagents",
        "headless": "only in non-interactive runs",
    }.get(when, "in every session")
    return PluginSpec(
        id=plugin.id,
        kind="prompt_section",
        title=plugin.display_title,
        description=plugin.description or "An authored prompt section.",
        group=plugin.group or "Prompt",
        source="authored",
        path=plugin.path,
        derived_from=plugin.derived_from,
        enabled_by_default=plugin.enabled_by_default,
        summary=f"Your own block of the system prompt, at order {plugin.order}."[:90],
        affects=("prompt",),
        audience="orchestrator",
        consequence=(
            f"Added to the composed system prompt {condition}, in {scope}, at "
            f"order {plugin.order}. Edits take effect in the next session: the "
            "prompt cache breakpoint sits on the system message, so its bytes "
            "must stay stable inside a session."
        ),
        docs_anchor="docs/design/AUTHORING.md",
        settings=(
            SettingSpec(
                key="body", type="text", default=plugin.prose, tier="free",
                title="Section text",
                help="The text this section contributes, verbatim.",
                affects=("prompt",),
                effect_detail="Composed with the internal sections in order, "
                              "joined by a blank line. A section that renders "
                              "empty is dropped.",
            ),
        ),
        metadata={
            "order": plugin.order,
            "authored": True,
            "scope": plugin.scope,
            "applies_to": list(plugin.applies_to),
            "when": when,
            "generated": False,
            "active": True,
            "editable": True,
        },
        view=lazy_view("markdown", plugin.prose, plugin.display_title, plugin.path),
    )


def authored_specs(plugins: Any, tools: dict[str, Any] | None = None) -> list[PluginSpec]:
    """One spec per authored plugin, every setting free, nothing required.

    ``agent`` files are absent on purpose: they arrive through ``load_defs``
    and are rendered by ``agent_specs`` like every other definition, which is
    the path agents were already on. Adding a second branch for them here would
    mean two specs for one file and a rule about which wins.
    """
    built = tools or {}
    out: list[PluginSpec] = []
    for plugin in plugins:
        try:
            if plugin.kind == "tool":
                tool = built.get(plugin.id) or plugin.to_tool()
                out.append(_authored_tool_spec(plugin, tool))
            elif plugin.kind == "prompt":
                out.append(_authored_prompt_spec(plugin))
        except Exception:  # one broken file must not hide the others
            continue
    return out
