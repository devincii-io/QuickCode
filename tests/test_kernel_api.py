"""The kernel-facing routes the configuration view reads.

``/api/prompt`` has two answers and has to keep them apart: live is the prompt
the next session starts from, resolved from the active composition exactly as
``manager.open()`` resolves it; ``?conv=`` is the bytes a running session is
being sent, which no file edit may change.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from quickcode.config import Config, Environment
from quickcode.core.events import TextDelta, TurnDone
from quickcode.providers.base import ModelInfo
from quickcode.server.app import create_app
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub


class ScriptedProvider:
    async def stream_chat(self, req):
        yield TextDelta("(done)")
        yield TurnDone("stop")

    async def list_models(self):
        return [ModelInfo(id="test/model", name="Test", context_length=100_000),
                ModelInfo(id="test/other", name="Other", context_length=8_000)]


def make_manager(cwd: Path) -> ConversationManager:
    cfg = Config()
    cfg.last_model = "test/model"
    env = Environment(
        cwd=str(cwd), platform="Windows", os_version="10", shell_name="bash",
        session_date="2026-08-17", is_git_repo=False, git_branch="",
    )
    return ConversationManager(cwd=cwd, config=cfg, env=env, provider=ScriptedProvider())


def make_client(manager: ConversationManager) -> TestClient:
    app = create_app(ProjectHub.from_manager(manager), host="127.0.0.1", port=8642,
                     token="")
    return TestClient(app, base_url="http://127.0.0.1:8642")


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def keeper(body: str, *, active: str = "keeper", spawns=None) -> dict:
    orchestrator: dict = {"section_bodies": {"prompt.tone": body}}
    if spawns is not None:
        orchestrator["spawns"] = spawns
    return {"active_preset": active,
            "presets": {"keeper": {"title": "Keeper", "orchestrator": orchestrator}}}


# --------------------------------------------------------------------------
# /api/prompt, live
# --------------------------------------------------------------------------

def test_the_live_prompt_is_the_one_the_next_session_is_sent(tmp_path):
    """Byte for byte. The Prompt page labels this "the prompt the next session
    starts from", and it used to read only the settings-layer bodies and always
    include the delegation playbook, so a composition's own rewrites never
    showed up there."""
    write_settings(tmp_path, keeper("Speak like a lighthouse keeper."))
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        live = client.get("/api/prompt").json()
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
    conv = manager.get(conv_id)

    assert "Speak like a lighthouse keeper." in live["text"]
    assert live["text"] == conv.agent.history.system_prompt
    assert live["frozen"] is False
    assert live["preset"] == "keeper"
    assert live["digest"] == conv.resolved.digest()


def test_a_composition_that_cannot_spawn_gets_no_delegation_playbook(tmp_path):
    write_settings(tmp_path, {"active_preset": "minimal"})
    with make_client(make_manager(tmp_path)) as client:
        live = client.get("/api/prompt").json()
    ids = [s["id"] for s in live["sections"]]
    assert "prompt.orchestration" not in ids
    assert "prompt.tool_use_policy" in ids


def test_section_offsets_address_the_text_they_describe(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        live = client.get("/api/prompt").json()
    text = live["text"]
    for section in live["sections"]:
        assert 0 <= section["start"] < section["end"] <= len(text)
    assert live["sections"][0]["start"] == 0
    assert live["sections"][-1]["end"] == len(text)


# --------------------------------------------------------------------------
# /api/prompt?conv=, frozen
# --------------------------------------------------------------------------

def test_the_frozen_prompt_is_what_the_session_is_sent_whatever_the_files_say(tmp_path):
    write_settings(tmp_path, keeper("Speak like a lighthouse keeper."))
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        write_settings(tmp_path, keeper("Speak like a ship's cat."))
        frozen = client.get(f"/api/prompt?conv={conv_id}").json()
        live = client.get("/api/prompt").json()
    conv = manager.get(conv_id)

    assert frozen["frozen"] is True
    assert frozen["conv"] == conv_id
    assert frozen["text"] == conv.agent.history.system_prompt
    assert "lighthouse keeper" in frozen["text"]
    assert "ship's cat" in live["text"] and "ship's cat" not in frozen["text"]
    # The session's own inputs reproduce its text, so its boundaries are exact.
    assert frozen["exact"] is True
    tone = next(s for s in frozen["sections"] if s["id"] == "prompt.tone")
    assert "lighthouse keeper" in frozen["text"][tone["start"]:tone["end"]]


def test_a_frozen_prompt_that_no_longer_re_renders_says_so(tmp_path):
    """An authored section added after the session opened changes a re-render,
    not the session. The boundaries are then located, and labelled as such."""
    manager = make_manager(tmp_path)
    with make_client(manager) as client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        plugins = tmp_path / ".quickcode" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "house-style.md").write_text(
            "---\nkind: prompt\nname: house-style\ntitle: House style\n"
            "after: prompt.conventions\n---\nAlways write British English.\n",
            encoding="utf-8",
        )
        frozen = client.get(f"/api/prompt?conv={conv_id}").json()
    conv = manager.get(conv_id)

    assert frozen["text"] == conv.agent.history.system_prompt
    assert "British English" not in frozen["text"]
    assert frozen["exact"] is False
    for section in frozen["sections"]:
        assert 0 <= section["start"] < section["end"] <= len(frozen["text"])
    assert "prompt.tool_use_policy" in [s["id"] for s in frozen["sections"]]


def test_an_unknown_conversation_is_a_404_not_the_live_prompt(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        res = client.get("/api/prompt?conv=nope")
    assert res.status_code == 404


# --------------------------------------------------------------------------
# facts the cards used to go without
# --------------------------------------------------------------------------

def _plugin(payload: dict, plugin_id: str) -> dict:
    return next(p for p in payload["plugins"] if p["id"] == plugin_id)


def test_the_active_provider_says_where_it_talks_and_how_many_models(tmp_path):
    manager = make_manager(tmp_path)
    manager.config.profile.base_url = "https://user:s3cret@llm.example.com:8443/v1/?key=abc"
    with make_client(manager) as client:
        before = client.get("/api/kernel").json()
        client.get("/api/models")        # loads the catalog
        after = client.get("/api/kernel").json()

    provider = f"provider.{manager.config.profile.provider}"
    md = _plugin(before, provider)["metadata"]
    # None, not 0: no catalog yet is not an empty catalog.
    assert md["model_count"] is None
    assert md["endpoint"] == "https://llm.example.com:8443/v1"
    assert _plugin(after, provider)["metadata"]["model_count"] == 2
    # The card is screenshot material; what identifies the endpoint stays and
    # what would let someone use it does not.
    assert "s3cret" not in json.dumps(after) and "key=abc" not in json.dumps(after)


def test_a_tool_carries_the_signature_its_card_shows(tmp_path):
    """One request for the Tools page instead of one per tool."""
    with make_client(make_manager(tmp_path)) as client:
        payload = client.get("/api/kernel").json()
        detail = client.get("/api/kernel/plugins/tool.read").json()

    signature = _plugin(payload, "tool.read")["metadata"]["signature"]
    schema = json.loads(detail["view"]["content"])
    assert signature.startswith("read(")
    for name in schema["parameters"]["properties"]:
        assert name in signature
    for plugin in payload["plugins"]:
        if plugin["kind"] == "tool":
            assert plugin["metadata"]["signature"].startswith(plugin["title"] + "(")


# --------------------------------------------------------------------------
# a new composition, named
# --------------------------------------------------------------------------

def test_a_named_composition_is_stored_under_that_name(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        made = client.post("/api/kernel/compositions/explore/derive",
                           json={"name": "Review only"})
        again = client.post("/api/kernel/compositions/standard/derive",
                            json={"name": "review only"})
        builtin = client.post("/api/kernel/compositions/explore/derive",
                              json={"name": "Standard"})
        unusable = client.post("/api/kernel/compositions/explore/derive",
                               json={"name": "???"})
        listed = {p["id"]: p for p in client.get("/api/presets").json()["presets"]}

    assert made.status_code == 200, made.text
    assert made.json()["id"] == "review-only"
    assert listed["review-only"]["title"] == "Review only"
    assert listed["review-only"]["tools"] == ["read", "glob", "grep"]
    # A typed name is a name somebody meant; a clash is refused, not renamed.
    assert again.status_code == 409 and "review-only" in again.json()["detail"]
    assert builtin.status_code == 409 and "built in" in builtin.json()["detail"]
    assert unusable.status_code == 400


def test_customise_without_a_name_numbers_its_copies(tmp_path):
    with make_client(make_manager(tmp_path)) as client:
        first = client.post("/api/kernel/compositions/standard/derive", json={}).json()
        second = client.post("/api/kernel/compositions/standard/derive", json={}).json()
    assert (first["id"], second["id"]) == ("standard-copy", "standard-copy-2")


# --------------------------------------------------------------------------
# a write keeps what the trust gate is only ignoring
# --------------------------------------------------------------------------

def test_editing_a_composition_in_an_untrusted_project_keeps_its_gated_fields(tmp_path):
    """The gate decides what a session *obeys*, not what the file *says*.

    An untrusted project's ``default_mode: auto-edit`` is ignored when a session
    opens -- correctly -- but the workbench wrote the preset back from that
    gated reading, so saving a tool edit erased the line from the file, and
    trusting the project afterwards found it gone.
    """
    write_settings(tmp_path, {
        "active_preset": "mine",
        "presets": {"mine": {"title": "Mine", "default_mode": "auto-edit",
                             "orchestrator": {"tools": ["read", "write"]}}},
    })
    with make_client(make_manager(tmp_path)) as client:
        res = client.put("/api/kernel/agents/%40orchestrator/composition",
                         json={"composition": {"tools": ["read"]}})
        copy = client.post("/api/kernel/compositions/mine/derive", json={"name": "Mine too"})
    assert res.status_code == 200, res.text
    assert copy.status_code == 200, copy.text

    on_disk = json.loads((tmp_path / ".quickcode" / "settings.json").read_text("utf-8"))
    assert on_disk["presets"]["mine"]["default_mode"] == "auto-edit"
    assert on_disk["presets"]["mine"]["orchestrator"]["tools"] == ["read"]
    assert on_disk["presets"]["mine-too"]["default_mode"] == "auto-edit"


# --------------------------------------------------------------------------
# duplicating the agent you are looking at
# --------------------------------------------------------------------------

def write_agent(cwd: Path, name: str, text: str) -> None:
    agents = cwd / ".quickcode" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(text, encoding="utf-8")


def test_an_agent_from_the_agents_directory_duplicates_into_a_file_you_can_edit(tmp_path):
    """Files in ``.quickcode/agents/`` predate the editor, which cannot open
    them; Duplicate was the way into it and answered 404 for every one."""
    write_agent(tmp_path, "reviewer",
                "---\nname: reviewer\ndescription: Reviews diffs.\n"
                "tools: [read, grep]\nspawns: [explore]\nmax_turns: 12\n---\n"
                "Review carefully.\n")
    with make_client(make_manager(tmp_path)) as client:
        res = client.post("/api/kernel/plugins/agent.reviewer/duplicate", json={})
        assert res.status_code == 200, res.text
        made = res.json()
        source = client.get(f"/api/kernel/authored/{made['plugin']['id']}/source").json()

    assert made["plugin"]["id"] == "agent.reviewer-copy"
    assert made["problems"] == []
    text = source["text"]
    for line in ("kind: agent", "name: reviewer-copy", "tools: [read, grep]",
                 "spawns: [explore]", "max_turns: 12", "derived_from: agent.reviewer"):
        assert line in text, line
    assert text.rstrip().endswith("Review carefully.")


def test_duplicating_a_replaced_built_in_copies_what_actually_runs(tmp_path):
    write_agent(tmp_path, "explore",
                "---\nname: explore\ndescription: looks familiar\n"
                "tools: [bash, write]\n---\nDo whatever you are asked.\n")
    with make_client(make_manager(tmp_path)) as client:
        made = client.post("/api/kernel/plugins/agent.explore/duplicate", json={}).json()
        source = client.get(f"/api/kernel/authored/{made['plugin']['id']}/source").json()
    assert "tools: [bash, write]" in source["text"]
    assert "Do whatever you are asked." in source["text"]
