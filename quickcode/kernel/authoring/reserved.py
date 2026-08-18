"""What an authored plugin may not be called, and why refusal beats shadowing.

``.quickcode/`` is committed. If a project-scope file could shadow ``tool.bash``
the way it shadows a user-scope file of its own, cloning a repository would
silently replace the shell tool with something that *looks* like the shell tool
-- a supply-chain hole with a friendly card UI on top. Nothing in the trajectory
would say "this ``read`` is not the ``read`` you think it is".

So a collision with an internal id is **refused**, not shadowed: the file
produces no plugin, and the problems surface offers the one action that is
actually wanted, which is Duplicate.

Collision between two *authored* plugins at different scopes is the ordinary
layering rule and is fine: project shadows user. Collision at the same scope is
``id_duplicate`` and skips both files, because guessing which of two was meant
is worse than loading neither.
"""

from __future__ import annotations

# Id prefixes an authored plugin can never claim. ``tool.``, ``agent.`` and
# ``prompt.`` are missing on purpose -- those are the three kinds it *is*.
RESERVED_ID_PREFIXES = (
    "runtime.", "hook.", "provider.", "policy.", "storage.", "panel.",
    "mcp.", "preset.",
)

# Wire names the model already knows. An authored tool taking one of these
# would be called in place of the real one.
RESERVED_WIRE_NAMES = frozenset({
    "read", "write", "edit", "glob", "grep", "bash", "plan",
    "agent", "send_message",
})

RESERVED_WIRE_PREFIXES = ("mcp__", "task_")


def internal_ids() -> set[str]:
    """Every plugin id QuickCode ships, without building a registry.

    Read off the live objects rather than tabulated: a tool added to
    ``core_tools`` is reserved the moment it exists, with nobody having to
    remember to add it here.
    """
    from quickcode.kernel import manifest
    from quickcode.prompts import sections as prompt_sections
    from quickcode.subagents.definitions import builtin_defs
    from quickcode.tools.registry import core_tools

    ids: set[str] = set()
    try:
        ids |= {spec.id for spec in manifest.core_specs()}
    except Exception:  # discovery must never take the app down
        pass
    try:
        ids |= {section.id for section in prompt_sections.SECTIONS}
    except Exception:
        pass
    try:
        ids |= {f"tool.{t.name}" for t in core_tools()}
    except Exception:
        pass
    try:
        ids |= {f"agent.{name}" for name in builtin_defs()}
    except Exception:
        pass
    return ids


def reserved_reason(plugin_id: str, kind: str, name: str) -> str:
    """Why this id is refused, in the user's vocabulary. "" means it is free."""
    for prefix in RESERVED_ID_PREFIXES:
        if plugin_id.startswith(prefix):
            return (f"ids starting with '{prefix}' belong to QuickCode's internals "
                    f"and cannot be authored")
    if plugin_id in internal_ids():
        return f"'{plugin_id}' is the id of a plugin QuickCode ships"
    if kind == "tool":
        if name in RESERVED_WIRE_NAMES:
            return f"'{name}' is the name of a built-in tool the model already calls"
        for prefix in RESERVED_WIRE_PREFIXES:
            if name.startswith(prefix):
                return f"tool names starting with '{prefix}' are reserved"
    return ""
