"""The explanation layer is complete for every plugin the registry builds.

Only completeness is worth asserting about prose: whether a sentence is *good*
is a review question, but whether it is *there* is a test question, and the two
fields whose absence leaves a dead end in the UI are ``locked_because`` and
``recourse`` on anything locked.
"""

from __future__ import annotations

from quickcode.kernel import manifest
from quickcode.kernel.registry import PluginRegistry


def _registry() -> PluginRegistry:
    """Every generator, fed the live objects, with nothing left out."""
    from quickcode.plugins import loader
    from quickcode.subagents.definitions import builtin_defs
    from quickcode.tools.registry import default_registry

    reg = PluginRegistry(None)
    reg.register_all(manifest.core_specs())
    reg.register_all(manifest.prompt_section_specs())
    reg.register_all(manifest.tool_specs(list(default_registry().tools.values())))
    reg.register_all(manifest.agent_specs(builtin_defs()))
    reg.register_all(manifest.provider_specs(loader.provider_factories()))
    reg.register_all(manifest.mcp_specs({"docs": {"command": "npx", "args": ["mcp-docs"]}}))
    return reg


def test_every_plugin_answers_the_six_questions():
    specs = _registry().all()
    assert len(specs) > 30

    for spec in specs:
        assert spec.summary, f"{spec.id} has no summary"
        assert len(spec.summary) <= 90, f"{spec.id} summary is {len(spec.summary)} chars"
        assert spec.affects, f"{spec.id} declares no affected surface"
        assert spec.consequence, f"{spec.id} does not say what changes"
        if spec.kind == "prompt_section":
            assert "prompt" in spec.affects, f"{spec.id} is a prompt section"

        if spec.tier() == "locked":
            assert spec.locked_because, f"{spec.id} is locked without a reason"
            assert spec.recourse, f"{spec.id} is locked without a way forward"

        for setting in spec.settings:
            if setting.tier != "locked":
                continue
            assert spec.locked_because_for(setting), (
                f"{spec.id}.{setting.key} is locked without a reason"
            )
            assert spec.recourse_for(setting), (
                f"{spec.id}.{setting.key} is locked without a way forward"
            )
