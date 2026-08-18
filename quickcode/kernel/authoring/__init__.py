"""Authoring: a markdown file in ``.quickcode/plugins/`` becomes a real plugin.

Three kinds are authorable without Python -- ``tool``, ``agent`` and
``prompt`` -- and each is one file, markdown with ``---`` frontmatter, holding
data the runtime already knows how to consume. Nothing here invents a new
execution path.

The organising rule: *the tier system protects QuickCode's internals from you;
it does not protect your own files from you.* An authored plugin is yours. It
has no locked settings, nothing is required, and you can delete it. What you
cannot do is reach into ``prompt.tool_use_policy`` and rewrite the contract the
loop depends on -- but you can stand your own section next to it, and you can
duplicate ``agent.explore`` into a file you own down to the byte.

===============  ==================================================
``format``       one parser for markdown + frontmatter + tagged blocks
``argv``         the substitution rules, pure and shared
``schema``       per-kind key tables and the validator
``model``        ``AuthoredPlugin`` and the converters to live objects
``reserved``     what an id may not be, and why refusal beats shadowing
``discovery``    scan, shadow, gate on trust; never raises
``store``        create, save, delete-to-trash, duplicate
``templates``    the commented examples New... writes
===============  ==================================================
"""

from quickcode.kernel.authoring.discovery import (
    Discovery,
    agent_defs,
    command_tools,
    discover,
    project_plugins_dir,
    prompt_sections,
    user_plugins_dir,
)
from quickcode.kernel.authoring.model import AuthoredPlugin, Param
from quickcode.kernel.authoring.schema import KINDS, validate

__all__ = [
    "KINDS",
    "AuthoredPlugin",
    "Discovery",
    "Param",
    "agent_defs",
    "command_tools",
    "discover",
    "project_plugins_dir",
    "prompt_sections",
    "user_plugins_dir",
    "validate",
]
