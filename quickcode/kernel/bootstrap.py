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


def _discover_authored(cwd: Path | None):
    from quickcode.kernel.authoring import discovery

    return discovery.discover(cwd)


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

    # Authored plugins first, because the tool list and the prompt bodies both
    # depend on what came out of it. Inside _safe: one malformed file must not
    # stop Settings rendering, must not stop the app starting, and must not
    # hide the plugins that are fine.
    authored = _safe(
        "authored plugins",
        lambda: _discover_authored(cwd),
        None,
    )
    authored_plugins = list(authored.plugins) if authored is not None else []
    authored_problems = list(authored.problems) if authored is not None else []
    authored_tools = _safe(
        "authored command tools",
        lambda: {p.id: p.to_tool() for p in authored_plugins if p.kind == "tool"},
        {},
    ) or {}

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
    authored_sections = _safe(
        "authored prompt sections",
        lambda: [p.to_prompt_section() for p in authored_plugins
                 if p.kind == "prompt" and "main" in p.applies_to],
        [],
    ) or []

    if env is not None and (not prompt_text or prompt_bodies is None):
        from quickcode.prompts.system import render_with_sections

        def _render():
            return render_with_sections(
                env, orchestration=True, extra_sections=authored_sections,
            )

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
    # Authored specs land *after* the internal ones, so a reserved-id collision
    # loses. Discovery already refuses those with ``id_reserved``; this is the
    # structural backstop, not the message.
    # A caller that already put the authored command tools in ``tools`` (the
    # session pool does) has had them described by ``tool_specs`` already, and
    # one plugin must have exactly one spec.
    described = {f"tool.{getattr(t, 'name', '')}" for t in tools}
    pending = [p for p in authored_plugins if p.id not in described]
    registry.register_all(
        _safe("authored specs",
              lambda: manifest.authored_specs(pending, authored_tools),
              [])
    )
    registry.add_problems(authored_problems)
    # Configuration written where nothing reads it is a silent no-op, which is
    # the one failure mode a settings screen must never have.
    from quickcode.kernel import state as state_store

    registry.add_problems(
        _safe("local settings", lambda: state_store.local_settings_problems(cwd), [])
    )
    return registry
