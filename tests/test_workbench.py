"""The agent workbench: /resolved, /preview, and session-scoped switching.

Four things are worth asserting here and the rest is rendering:

1. ``/resolved`` answers with what the runner actually builds -- the same tool
   objects and the same prompt bytes, for the orchestrator and for a spawned
   child alike. This is the guard against the endpoint becoming a plausible
   second implementation.
2. ``/preview`` renders through the real prompt code and writes nothing.
3. A switch is refused while the agent is busy, with the reason.
4. A switch records a ``composition`` meta record, and resume restores the
   composition the session *ended* with.

No test here reaches a provider: every response is a local scripted generator.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.core.events import TextDelta, TurnDone
from quickcode.providers.base import ModelInfo
from quickcode.server.agents_api import register_agent_routes
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub
from quickcode.subagents.runner import spawn_subagent


class ScriptedProvider:
    """One canned assistant message per request. Never touches a network."""

    async def stream_chat(self, req):
        yield TextDelta("(done)")
        yield TurnDone("stop")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000)]


def make_env(cwd: Path) -> Environment:
    return Environment(
        cwd=str(cwd), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )


def make_manager(tmp_path: Path) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    return ConversationManager(
        cwd=tmp_path, config=cfg, env=make_env(tmp_path), provider=ScriptedProvider(),
    )


def make_client(manager: ConversationManager) -> TestClient:
    hub = ProjectHub.from_manager(manager)
    app = create_app(hub, host="127.0.0.1", port=8642, token="")
    # The one line app.py needs; registered here so the tests exercise exactly
    # the same wiring.
    register_agent_routes(app, hub)
    return TestClient(app, base_url="http://127.0.0.1:8642")


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


DELEGATOR = {
    "active_preset": "standard",
    "presets": {
        "delegator": {
            "title": "Delegator",
            "description": "Plans and delegates; touches nothing itself.",
            "orchestrator": {
                "tools": ["read", "glob", "grep"],
                "spawns": ["explore", "general"],
            },
        },
    },
}


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------

def test_the_orchestrator_is_a_first_class_agent_in_the_inventory(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        payload = client.get("/api/kernel/agents").json()
    ids = [a["id"] for a in payload["agents"]]
    assert ids[0] == "@orchestrator"
    assert "explore" in ids and "general" in ids
    orchestrator = payload["agents"][0]
    assert orchestrator["role"] == "orchestrator"
    assert orchestrator["tool_count"] > 0


# --------------------------------------------------------------------------
# /resolved is the runner's answer, not a second one
# --------------------------------------------------------------------------

def test_resolved_for_the_orchestrator_matches_the_running_session(tmp_path):
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)
        payload = client.get(
            f"/api/kernel/agents/%40orchestrator/resolved?conv={conv_id}"
        ).json()

    assert payload["frozen"] is True
    assert payload["digest"] == conv.resolved.digest()
    # The same tool objects, in the same order, as the session's registry.
    assert [t["name"] for t in payload["tools"]] == list(conv.agent.registry.tools)
    # The same bytes the model is being sent right now.
    assert payload["prompt"]["text"] == conv.agent.history.system_prompt
    # Every granted tool carries a chain entry saying where it came from.
    chain = payload["resolved"]["chain"]
    for name in payload["resolved"]["tools"]:
        assert chain.get(f"tools.{name}"), name


def test_denied_tools_are_listed_with_a_reason_never_omitted(tmp_path):
    write_settings(tmp_path, {
        "active_preset": "explore-only",
        "presets": {
            "explore-only": {"title": "Explore only",
                             "orchestrator": {"tools": ["read", "glob", "grep"]}},
        },
    })
    with make_client(make_manager(tmp_path)) as client:
        payload = client.get("/api/kernel/agents/%40orchestrator/resolved").json()

    denied = {row["name"]: row for row in payload["denied"]}
    assert "write" in denied and "bash" in denied
    assert denied["write"]["reason"]
    # The delegation pair is never a checkbox: it says so rather than looking
    # like something you forgot to tick.
    rows = {row["name"]: row for row in payload["pool"]}
    assert rows["agent"]["state"] == "excluded"
    assert "by depth" in rows["agent"]["reason"]


def test_resolved_for_a_subagent_matches_what_a_real_spawn_builds(tmp_path):
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)
        deps = conv.agent.ctx.extra["subagent"]
        agent_id, _report = asyncio.run(
            spawn_subagent(deps, agent_type="explore", prompt="look around")
        )
        child = deps.roster[agent_id]
        payload = client.get("/api/kernel/agents/explore/resolved").json()

    assert [t["name"] for t in payload["tools"]] == list(child.registry.tools)
    assert payload["prompt"]["text"] == child.history.system_prompt
    assert payload["role"] == "subagent"
    # A subagent's prompt comes from a different template, and the payload says
    # so rather than leaving it to be discovered.
    assert any(a["id"] == "prompt.*" for a in payload["prompt"]["absences"])
    assert any("skip_project_instructions" in a["reason"]
               for a in payload["prompt"]["absences"])


def test_the_depth_zero_carve_out_is_visible_in_the_workbench(tmp_path):
    write_settings(tmp_path, {**DELEGATOR, "active_preset": "delegator"})
    with make_client(make_manager(tmp_path)) as client:
        orchestrator = client.get(
            "/api/kernel/agents/%40orchestrator/resolved").json()
        general = client.get("/api/kernel/agents/general/resolved").json()

    assert "write" not in orchestrator["resolved"]["tools"]
    # The orchestrator's restriction states what it does with its own hands; a
    # child at depth 0 still draws from the session pool.
    assert "write" in general["resolved"]["tools"]


def test_the_footer_counts_what_a_person_actually_asks(tmp_path):
    write_settings(tmp_path, {**DELEGATOR, "active_preset": "delegator"})
    with make_client(make_manager(tmp_path)) as client:
        payload = client.get("/api/kernel/agents/%40orchestrator/resolved").json()
    # "3 tools" says nothing; this sentence is what a person came to find out.
    assert payload["footer"].endswith(
        "0 that change files · 0 that run shell commands"
    )
    assert payload["footer"].startswith("5 tools · 5 read-only")


# --------------------------------------------------------------------------
# /preview
# --------------------------------------------------------------------------

def test_preview_renders_the_same_bytes_the_session_would_send(tmp_path):
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)
        preview = client.post("/api/kernel/agents/%40orchestrator/preview",
                              json={}).json()
    # An empty draft is the saved state, so the preview must be byte-identical
    # to what the runner rendered. This is the guard against the preview
    # becoming a reconstruction of prompts/sections.py in a second language.
    assert preview["prompt"]["text"] == conv.agent.history.system_prompt
    assert preview["frozen"] is False
    assert preview["draft"] is True


def test_a_draft_prompt_body_changes_the_preview_and_writes_nothing(tmp_path):
    settings = tmp_path / ".quickcode" / "settings.json"
    with make_client(make_manager(tmp_path)) as client:
        saved = client.get("/api/kernel/agents/explore/resolved").json()
        preview = client.post(
            "/api/kernel/agents/explore/preview",
            json={"prompt_body": "You only ever read one file and then stop."},
        ).json()

    assert "You only ever read one file" in preview["prompt"]["text"]
    assert preview["prompt"]["text"] != saved["prompt"]["text"]
    assert not settings.exists()
    assert not (tmp_path / ".quickcode" / "agents").exists()


def test_a_draft_tool_pattern_changes_the_grant_and_the_schemas(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        preview = client.post(
            "/api/kernel/agents/explore/preview",
            json={"composition": {"tools": ["read"]}},
        ).json()
    names = [t["name"] for t in preview["tools"]]
    assert "read" in names and "glob" not in names and "write" not in names
    rows = {row["name"]: row for row in preview["pool"]}
    assert rows["read"]["state"] == "matched"
    assert rows["read"]["pattern"] == "read"
    assert rows["glob"]["state"] == "unmatched"
    assert rows["glob"]["reason"]


def test_a_glob_pattern_is_reported_as_glob_matched_not_frozen(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        preview = client.post(
            "/api/kernel/agents/explore/preview",
            json={"composition": {"tools": ["task_*"]}},
        ).json()
    rows = {row["name"]: row for row in preview["pool"]}
    assert rows["task_create"]["state"] == "matched-by-glob"
    assert rows["task_create"]["pattern"] == "task_*"


# --------------------------------------------------------------------------
# session-scoped switching
# --------------------------------------------------------------------------

def test_a_switch_is_refused_while_the_agent_is_busy(tmp_path):
    manager = make_manager(tmp_path)
    write_settings(tmp_path, DELEGATOR)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)

        class _Busy:
            busy = True

            def __getattr__(self, name):
                return getattr(conv.agent, name)

        conv.agent = _Busy()  # type: ignore[assignment]
        res = client.post(f"/api/kernel/conversations/{conv_id}/composition",
                          json={"preset": "delegator"})
    assert res.status_code == 409
    # Refused with the reason, not queued and not silently applied later.
    assert "turn boundary" in res.json()["detail"]


def test_a_switch_records_the_composition_and_resume_restores_it(tmp_path):
    manager = make_manager(tmp_path)
    write_settings(tmp_path, DELEGATOR)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.get(conv_id)
        before = conv.resolved.digest()

        res = client.post(f"/api/kernel/conversations/{conv_id}/composition",
                          json={"preset": "delegator"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["preset"] == "delegator"
        assert "write" in body["lost"]

        assert conv.preset_id == "delegator"
        assert conv.resolved.digest() != before
        # The tools the model is handed moved with it, not just the payload.
        assert "write" not in conv.agent.registry.tools

        meta = conv.store.meta()
        assert meta["preset"] == "delegator"
        assert meta["composition"]["digest"] == conv.resolved.digest()

        # A transcript marker, because the same conversation genuinely had two
        # different agents in it and a log without one would mislead.
        kinds = [e.get("type") for e in conv.store.replay_events()]
        assert "composition_changed" in kinds

        # Resume restores the composition the session ENDED with, not the one
        # it started with.
        ended_with = conv.resolved.digest()
        manager.conversations.pop(conv_id)
        client.post("/api/conversations", json={"resume": conv_id})
        reopened = manager.get(conv_id)

    assert reopened is not None
    assert reopened.preset_id == "delegator"
    assert reopened.resolved.digest() == ended_with
    assert "write" not in reopened.agent.registry.tools


def test_switching_to_the_composition_already_running_is_refused(tmp_path):
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        res = client.post(f"/api/kernel/conversations/{conv_id}/composition",
                          json={"preset": "standard"})
    assert res.status_code == 409
    assert "already runs" in res.json()["detail"]


# --------------------------------------------------------------------------
# duplicate-to-customise
# --------------------------------------------------------------------------

def test_customise_this_duplicates_a_builtin_into_the_project(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        created = client.post("/api/kernel/compositions/standard/derive",
                              json={}).json()
        assert created["id"] == "standard-copy"
        listed = client.get("/api/presets").json()
    ids = [p["id"] for p in listed["presets"]]
    assert "standard-copy" in ids
    # The original is untouched.
    assert "standard" in ids


def test_editing_a_builtin_composition_is_refused_with_the_recourse(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        res = client.put("/api/kernel/agents/%40orchestrator/composition",
                         json={"composition": {"tools": ["read"]}})
    assert res.status_code == 409
    assert "Duplicate" in res.json()["detail"]


def test_a_derived_composition_accepts_a_tool_pattern_edit(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        client.post("/api/kernel/compositions/standard/derive", json={})
        client.put("/api/presets/active", json={"preset": "standard-copy"})
        res = client.put("/api/kernel/agents/%40orchestrator/composition",
                         json={"composition": {"tools": ["read", "glob"]}})
        assert res.status_code == 200, res.text
        after = client.get("/api/kernel/agents/%40orchestrator/resolved").json()
    tools = after["resolved"]["tools"]
    assert "read" in tools and "glob" in tools
    assert "write" not in tools and "bash" not in tools
    # The patterns round-trip as patterns, never expanded into a frozen list.
    assert after["grant"]["patterns"] == ["read", "glob"]
    assert after["grant"]["editable"] is True
