"""One plugin per configured MCP server."""

from __future__ import annotations

import json
from typing import Any

from quickcode.kernel.manifest._text import lazy_view
from quickcode.kernel.spec import PluginSpec

REDACTED = "•" * 8


def _redacted(cfg: dict[str, Any]) -> dict[str, Any]:
    """One MCP server's config with its ``env`` *values* replaced.

    An MCP server is configured by handing it credentials in ``env``, so this
    block routinely holds a live API token. It is rendered on a card anyone can
    open, and again inside the trust banner, which is the one moment the user is
    most likely to be sharing their screen -- reviewing a project before
    granting it.

    The key names stay, because they are what the review is actually for: you
    are deciding whether this server should receive a token at all, and the name
    tells you that. The value tells you nothing you can check and is the part
    that must not be read over a shoulder or captured in a screenshot.

    The banner's own list view already printed only the key names. This makes
    the raw block agree with it rather than quietly undo it.
    """
    if not isinstance(cfg.get("env"), dict):
        return cfg
    return {**cfg, "env": {k: REDACTED for k in cfg["env"]}}


def mcp_specs(configs: dict[str, dict[str, Any]]) -> list[PluginSpec]:
    out: list[PluginSpec] = []
    for name, cfg in sorted(configs.items()):
        command = " ".join([str(cfg.get("command", ""))] + list(cfg.get("args", []))).strip()
        out.append(PluginSpec(
            id=f"mcp.{name}",
            kind="mcp_server",
            title=name,
            description=command or "External MCP server.",
            group="MCP",
            source="config",
            summary="An external process whose tools join this project's tool pool.",
            affects=("tool_list",),
            audience="all_agents",
            consequence="Started once per QuickCode run, not per session. Its tools "
                        "are named mcp__<server>__<tool> and go through the same "
                        "permission gate as the built-ins; a tool that does not "
                        "declare itself read-only is prompted for.",
            docs_anchor="docs/ARCHITECTURE.md#the-plugin-kernel",
            metadata={"server": name, "command": cfg.get("command", ""),
                      "args": list(cfg.get("args", []))},
            view=lazy_view("json", json.dumps(_redacted(cfg), indent=2),
                           f"{name} definition"),
        ))
    return out
