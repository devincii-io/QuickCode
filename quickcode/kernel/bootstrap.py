"""Assemble a plugin registry for one project.

This is the single place that answers "what does this install actually
consist of". Every argument is optional and discovered from the live system
when omitted; callers that already built a tool registry (the server does)
pass it in, so the UI describes the tools the agent really has rather than a
freshly built lookalike.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from quickcode.kernel import manifest
from quickcode.kernel.registry import PluginRegistry
from quickcode.kernel.spec import PluginView

log = logging.getLogger("quickcode.kernel.bootstrap")


def _safe(label: str, fn, default):
    """Discovery must never take Settings down: a broken source is logged."""
    try:
        return fn()
    except Exception as exc:
        log.warning("plugin discovery failed for %s: %s", label, exc)
        return default


def build_registry(
    cwd: Path | None = None,
    *,
    tools: list[Any] | None = None,
    agent_defs: dict[str, Any] | None = None,
    providers: dict[str, Any] | None = None,
    mcp_configs: dict[str, dict[str, Any]] | None = None,
    prompt_text: str = "",
    prompt_bodies: dict[str, str] | None = None,
    env: object | None = None,
    active_provider: str = "",
) -> PluginRegistry:
    from quickcode.plugins import loader
    from quickcode.tools.registry import default_registry

    registry = PluginRegistry(cwd)

    if tools is None:
        live = _safe("tool registry", lambda: default_registry(), None)
        tools = list(live.tools.values()) if live is not None else []

    if agent_defs is None and cwd is not None:
        from quickcode.subagents.definitions import load_defs

        agent_defs = _safe("agent definitions", lambda: load_defs(Path(cwd)), {})

    if providers is None:
        providers = _safe("provider factories", loader.provider_factories, {})

    if mcp_configs is None and cwd is not None:
        from quickcode.plugins.mcp import load_server_configs

        mcp_configs = _safe("mcp servers", lambda: load_server_configs(Path(cwd)), {})

    # With an Environment we can render the real prompt for this project, so
    # the Prompt section of Settings shows what the agent is actually told.
    if env is not None and (not prompt_text or prompt_bodies is None):
        from quickcode.prompts.system import render_with_sections

        def _render():
            return render_with_sections(env, orchestration=True)

        composed, rendered = _safe("system prompt", _render, ("", []))
        prompt_text = prompt_text or composed
        if prompt_bodies is None:
            prompt_bodies = {s.id: s.text for s in rendered}

    prompt_view = None
    if prompt_text:
        prompt_view = lambda: PluginView(  # noqa: E731 - a one-line thunk
            format="text", content=prompt_text, title="Composed system prompt"
        )

    registry.register_all(manifest.core_specs(prompt_view=prompt_view))
    registry.register_all(manifest.prompt_section_specs(prompt_bodies))
    registry.register_all(manifest.tool_specs(tools))
    registry.register_all(manifest.agent_specs(agent_defs or {}))
    registry.register_all(manifest.provider_specs(providers or {}, active=active_provider))
    registry.register_all(manifest.mcp_specs(mcp_configs or {}))
    # Configuration written where nothing reads it is a silent no-op, which is
    # the one failure mode a settings screen must never have.
    from quickcode.kernel import state as state_store

    registry.add_problems(
        _safe("local settings", lambda: state_store.local_settings_problems(cwd), [])
    )
    return registry
