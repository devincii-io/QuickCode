"""One session-assembly path, for the app and for ``quickcode -p``.

``ConversationManager.open()`` and the headless CLI each built the session's own
agent, and ``-p`` had drifted from the app: it ran the unfiltered default
registry, so a disabled plugin, the active composition and that composition's
spawn list meant nothing there; it rendered its prompt from other inputs; its
session record carried no composition, so ``--continue`` could not keep one;
and its subagents were resolved against no pool, no parent and no definitions.

Everything here drives the real CLI entry point with a scripted provider, and
where it matters compares the result with what the app opens on the same
project.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from quickcode import cli
from quickcode.config import Config
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.kernel.composition import Resolved
from quickcode.session.store import SessionStore
from tests.test_headless import _headless, _install
from tests.test_server import FakeProvider, make_manager

SUBAGENT_MARKER = "QuickCode subagent"


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def offered(request) -> set[str]:
    return {t.name for t in request.tools}


def system_of(request) -> str:
    return next((m.content for m in request.messages if m.role == "system"), "") or ""


def meta(cwd: Path) -> dict:
    return SessionStore(cwd, SessionStore.most_recent(cwd)).meta()


def events(cwd: Path) -> list[dict]:
    return SessionStore(cwd, SessionStore.most_recent(cwd)).load_events()


class Delegating(FakeProvider):
    """The session's own agent follows its script; every subagent answers at
    once, and the requests each one made are kept apart for inspection."""

    def __init__(self, scripts):
        super().__init__(scripts)
        self.child_requests = []

    async def stream_chat(self, req):
        if SUBAGENT_MARKER in system_of(req):
            self.child_requests.append(req)
            yield TextDelta("nothing to report")
            yield TurnDone("stop")
            return
        async for ev in super().stream_chat(req):
            yield ev


def spawn(agent_type: str) -> list:
    call = json.dumps({"description": "look", "prompt": "look", "agent_type": agent_type})
    return [
        [ToolCallEnd(id="a1", name="agent", arguments=call), TurnDone("tool_calls")],
        [TextDelta("done"), TurnDone("stop")],
    ]


SLIM = {
    "active_preset": "slim",
    "presets": {"slim": {
        "title": "Slim",
        "orchestrator": {"tools": ["read", "glob", "grep", "write", "edit"],
                         "spawns": ["explore"]},
        "prompt_overrides": {"prompt.tone": "Answer in exactly one haiku."},
    }},
    "plugins": {"tool.write": {"enabled": False}},
}


# ------------------------------------------------------------ the composition


def test_a_headless_run_offers_the_tools_its_composition_and_plugins_allow(
    tmp_path, monkeypatch, capsys
):
    """The preset removed bash; the plugin toggle removed write. ``-p`` used to
    hand the model both, from the default registry."""
    write_settings(tmp_path, SLIM)
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "hello"))
    assert capsys.readouterr().out.strip() == "ok"

    tools = offered(provider.requests[0])
    assert {"read", "glob", "grep", "edit", "agent"} <= tools
    assert "bash" not in tools            # the preset left it out
    assert "write" not in tools           # the plugin is switched off


def test_a_headless_prompt_is_rendered_from_the_composition(tmp_path, monkeypatch, capsys):
    """A preset's section body reached the app's prompt and not ``-p``'s, which
    read only the plugin-state overrides; and ``-p`` always claimed it could
    delegate, even under a composition that spawns nothing."""
    write_settings(tmp_path, {
        "active_preset": "solo",
        "presets": {"solo": {
            "title": "Solo",
            "orchestrator": {"tools": ["read", "glob", "grep"], "spawns": []},
            "prompt_overrides": {"prompt.tone": "Answer in exactly one haiku."},
        }},
    })
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()

    system = system_of(provider.requests[0])
    assert "Answer in exactly one haiku." in system
    assert "<orchestration>" not in system
    assert "<headless_mode>" in system
    assert "agent" not in offered(provider.requests[0])


def test_a_headless_session_records_its_composition(tmp_path, monkeypatch, capsys):
    write_settings(tmp_path, SLIM)
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()

    recorded = meta(tmp_path)
    assert recorded["preset"] == "slim"
    frozen = Resolved.from_json(recorded["composition"])
    assert frozen is not None
    assert set(frozen.tools) == offered(provider.requests[0])


def test_continue_keeps_the_composition_a_headless_session_started_with(
    tmp_path, monkeypatch, capsys
):
    """What the app does on resume, and for the same reason: the conversation
    was already told what tools it has."""
    write_settings(tmp_path, SLIM)
    first = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])
    _install(monkeypatch, first)
    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()

    write_settings(tmp_path, {})           # the preset is gone, bash is back
    second = FakeProvider([[TextDelta("still ok"), TurnDone("stop")]])
    _install(monkeypatch, second)
    cli.main(_headless(tmp_path, "--continue", "and again"))
    assert capsys.readouterr().out.strip() == "still ok"

    assert offered(second.requests[0]) == offered(first.requests[0])
    assert "bash" not in offered(second.requests[0])


# ------------------------------------------------------------------ subagents


def test_a_headless_run_may_spawn_only_what_its_composition_lists(
    tmp_path, monkeypatch, capsys
):
    """``slim`` spawns explore and nothing else. ``-p`` gave its subagents no
    parent composition, so the list was never checked there."""
    write_settings(tmp_path, SLIM)
    provider = Delegating(spawn("general"))
    _install(monkeypatch, provider)

    cli.main(_headless(tmp_path, "delegate"))
    capsys.readouterr()

    assert provider.child_requests == []
    result = next(e for e in events(tmp_path) if e["type"] == "tool_result")
    assert result["is_error"] is True
    assert "may not spawn 'general'" in result["content"]


def test_a_headless_subagent_is_resolved_against_the_session_pool(
    tmp_path, monkeypatch, capsys
):
    """A switched-off plugin is off for every agent at every depth. A ``-p``
    child was resolved against the whole default registry and got it back."""
    write_settings(tmp_path, {"plugins": {"tool.write": {"enabled": False}}})
    provider = Delegating(spawn("general"))
    built: dict = {}
    _install(monkeypatch, provider, built)

    cli.main(_headless(tmp_path, "delegate"))
    capsys.readouterr()

    assert len(provider.child_requests) == 1
    child_tools = offered(provider.child_requests[0])
    assert {"read", "edit", "bash"} <= child_tools
    assert "write" not in child_tools

    deps = built["agent"].ctx.extra["subagent"]
    assert "write" not in {t.name for t in deps.pool}
    assert deps.parent is not None and deps.parent.id == "@orchestrator"
    assert deps.defs is not None and "general" in deps.defs
    assert deps.preset is not None


# ------------------------------------------------------------ parity with the app


def test_the_app_and_a_headless_run_assemble_the_same_session(
    tmp_path, monkeypatch, capsys
):
    write_settings(tmp_path, SLIM)
    built: dict = {}
    _install(monkeypatch, FakeProvider([[TextDelta("ok"), TurnDone("stop")]]), built)
    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()
    headless = built["agent"]
    headless_deps = headless.ctx.extra["subagent"]
    recorded = Resolved.from_json(meta(tmp_path)["composition"])

    async def the_app():
        manager = make_manager(tmp_path, FakeProvider([]))
        conv = manager.open()
        try:
            assert set(headless.registry.tools) == set(conv.agent.registry.tools)
            assert headless.permissions.specs == conv.agent.permissions.specs
            assert recorded is not None and recorded.digest() == conv.resolved.digest()
            app_deps = conv.agent.ctx.extra["subagent"]
            assert ({t.name for t in headless_deps.pool}
                    == {t.name for t in app_deps.pool})
            assert headless_deps.limits == app_deps.limits
        finally:
            await manager.close()

    asyncio.run(the_app())


def test_a_headless_prompt_names_the_backend_the_way_the_app_does(
    tmp_path, monkeypatch, capsys
):
    """``-p`` named the provider from the raw base URL, so a profile on the
    native Anthropic provider told the model it was served through OpenRouter."""
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])

    def load(cls=None, *a, **k):
        cfg = Config()
        cfg.last_model = "test/model"
        cfg.profile.provider = "anthropic"
        return cfg

    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: load()))
    monkeypatch.setattr("quickcode.plugins.loader.make_provider", lambda *a, **k: provider)

    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()

    system = system_of(provider.requests[0])
    assert "served through Anthropic" in system
    assert "OpenRouter" not in system


def test_a_headless_run_sends_the_users_generation_settings(tmp_path, monkeypatch, capsys):
    """The response budget in particular is not a preference: the provider
    reserves credit against it. The app applied it; ``-p`` did not."""
    provider = FakeProvider([[TextDelta("ok"), TurnDone("stop")]])

    def load(cls=None, *a, **k):
        cfg = Config()
        cfg.last_model = "test/model"
        cfg.max_tokens = 2048
        cfg.temperature = 0.25
        return cfg

    _install(monkeypatch, provider)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: load()))

    cli.main(_headless(tmp_path, "hello"))
    capsys.readouterr()

    assert provider.requests[0].max_tokens == 2048
    assert provider.requests[0].temperature == 0.25
