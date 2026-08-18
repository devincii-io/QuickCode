"""``used_by``: the reverse index behind the USED BY block.

Two things are worth testing here and nothing else is. First the answer: for a
tool, ``used_by`` has to name every composition whose orchestrator ends up
holding it *and* every agent definition that lists it -- the resolved answer on
one side, the declared one on the other, because those are the two files you
would go and edit. Second the cache: the index is expensive enough to need one
and the server rebuilds the registry per request on purpose, so a cache that
outlived a request would answer with the composition you had before your edit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.kernel import build_registry
from quickcode.kernel.registry import PluginRegistry


@pytest.fixture(autouse=True)
def isolate_user_dirs(tmp_path, monkeypatch):
    """Neither the developer's presets nor their agents are a test fixture."""
    fake_settings = tmp_path / "user-settings.json"
    monkeypatch.setattr(
        "quickcode.kernel.state.user_settings_path", lambda: fake_settings)
    monkeypatch.setattr(
        "quickcode.kernel.preset.user_settings_path", lambda: fake_settings)
    monkeypatch.setattr(
        "quickcode.subagents.definitions._USER_DIR", tmp_path / "user-agents")
    return fake_settings


@pytest.fixture()
def project(tmp_path) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".quickcode").mkdir(parents=True)
    return cwd


def write_settings(cwd: Path, body: dict) -> None:
    path = cwd / ".quickcode" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def write_agent(cwd: Path, name: str, frontmatter: str) -> None:
    d = cwd / ".quickcode" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\n{frontmatter}---\n\nDo the thing.\n", encoding="utf-8")


def sources(registry: PluginRegistry, plugin_id: str) -> set[tuple[str, str]]:
    return {(u.kind, u.id) for u in registry.used_by(plugin_id)}


# --------------------------------------------------------------------------
# the answer
# --------------------------------------------------------------------------

def test_used_by_bash_names_every_granting_preset_and_listing_agent(project):
    """The test the plan names, in full.

    Three built-in compositions: ``standard`` inherits everything and so grants
    bash, ``minimal`` lists it, ``explore`` is read/glob/grep and must not
    appear. One project preset that revokes it must not appear either. Two
    project agents: one lists bash, one does not.
    """
    write_settings(project, {
        "presets": {
            "shell": {"title": "Shell", "orchestrator": {"tools": ["bash", "read"]}},
            "noshell": {"title": "No shell", "orchestrator": {"tools": ["read"]}},
        }
    })
    write_agent(project, "builder", "tools: bash, read\n")
    write_agent(project, "reader", "tools: read, grep\n")

    registry = build_registry(project)
    found = sources(registry, "tool.bash")

    assert ("composition", "standard") in found   # inherits the whole pool
    assert ("composition", "minimal") in found    # names it
    assert ("composition", "shell") in found      # names it, from the project file
    assert ("agent", "builder") in found          # lists it

    assert ("composition", "explore") not in found   # read/glob/grep only
    assert ("composition", "noshell") not in found
    assert ("agent", "reader") not in found
    assert ("agent", "explore") not in found         # built-in: read/glob/grep

    # And every entry is a link to a page that exists.
    for use in registry.used_by("tool.bash"):
        assert use.href.startswith("#/config/")
        assert use.via and use.title


def test_used_by_follows_a_glob_and_says_which_pattern_matched(project):
    write_agent(project, "wide", "tools: gl*b, read\n")
    registry = build_registry(project)

    entry = next(u for u in registry.used_by("tool.glob")
                 if u.kind == "agent" and u.id == "wide")
    assert "gl*b" in entry.via
    # A literal is described as a literal, not as a pattern.
    plain = next(u for u in registry.used_by("tool.read")
                 if u.kind == "agent" and u.id == "wide")
    assert "gl*b" not in plain.via


def test_used_by_covers_spawns_bases_and_bindings(project):
    write_settings(project, {
        "presets": {
            "bound": {
                "title": "Bound",
                "orchestrator": {"tools": ["read"], "spawns": ["explore"]},
                "bindings": [
                    {"plugin": "tool.bash", "to": "@subagents", "effect": "grant"},
                ],
            }
        }
    })
    write_agent(project, "derived", "base: explore\nspawns: explore\n")
    registry = build_registry(project)

    # A binding is a statement about a relationship, and it is the composition
    # that owns it -- so it shows up under the composition, not under the agent.
    binding = next(u for u in registry.used_by("tool.bash") if u.id == "bound")
    assert "binding" in binding.via and "@subagents" in binding.via

    on_explore = sources(registry, "agent.explore")
    assert ("composition", "bound") in on_explore
    assert ("agent", "derived") in on_explore
    bases = [u.via for u in registry.used_by("agent.explore")
             if u.kind == "agent" and u.id == "derived"]
    assert any("base" in v or "spawn" in v for v in bases)


def test_used_by_is_on_the_plugin_payload_and_is_json_safe(project):
    registry = build_registry(project)
    payload = registry.plugin_json("tool.bash")
    assert isinstance(payload["used_by"], list)
    assert payload["used_by"], "tool.bash is granted by the standard composition"
    row = payload["used_by"][0]
    assert set(row) == {"kind", "id", "title", "via", "href"}
    json.dumps(payload)  # must survive the wire


def test_unknown_names_never_become_dead_links(project):
    """A preset naming a tool this install does not have gets no row."""
    write_settings(project, {
        "presets": {"ghost": {"orchestrator": {"tools": ["not_a_real_tool"]}}}
    })
    registry = build_registry(project)
    assert registry.used_by("tool.not_a_real_tool") == []
    assert all("not_a_real_tool" not in u.href for u in registry.used_by("tool.read"))


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------

def test_the_index_is_built_once_per_registry(project, monkeypatch):
    """The Parts list renders 37 plugins; it must not resolve 37 times."""
    registry = build_registry(project)
    calls = []
    original = PluginRegistry._build_used_by
    monkeypatch.setattr(
        PluginRegistry, "_build_used_by",
        lambda self: (calls.append(1), original(self))[1])

    registry.to_json()
    registry.used_by("tool.bash")
    registry.plugin_json("tool.read")
    assert calls == [1]


def test_the_cache_does_not_outlive_the_registry_that_built_it(project):
    """The failure this guards: an edit, then a stale answer on the next request.

    ``_registry_for`` rebuilds per request precisely so a settings edit shows
    up immediately. If the index were memoised on the module (or on the class)
    the second registry would serve the first one's answer.
    """
    write_settings(project, {
        "presets": {"only": {"title": "Only", "orchestrator": {"tools": ["bash"]}}}
    })
    first = build_registry(project)
    assert ("composition", "only") in sources(first, "tool.bash")

    # The user edits the preset. A new request, a new registry.
    write_settings(project, {
        "presets": {"only": {"title": "Only", "orchestrator": {"tools": ["read"]}}}
    })
    second = build_registry(project)
    assert ("composition", "only") not in sources(second, "tool.bash")
    assert ("composition", "only") in sources(second, "tool.read")
    # And the first one is untouched: it is a snapshot of the request it served.
    assert ("composition", "only") in sources(first, "tool.bash")


def test_two_projects_do_not_share_an_answer(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for d in (a, b):
        (d / ".quickcode").mkdir(parents=True)
    write_settings(a, {"presets": {"p": {"orchestrator": {"tools": ["bash"]}}}})
    write_settings(b, {"presets": {"p": {"orchestrator": {"tools": ["read"]}}}})

    assert ("composition", "p") in sources(build_registry(a), "tool.bash")
    assert ("composition", "p") not in sources(build_registry(b), "tool.bash")


def test_the_endpoint_reflects_an_edit_made_between_two_requests(tmp_path):
    """The same claim, at the layer that actually serves it.

    ``_registry_for`` builds a registry per request. Two GETs with a settings
    write between them must disagree -- that is what a per-request cache buys
    and what a process-level one would take away. No provider is ever called:
    the scripted fake is here to construct the manager, not to answer.
    """
    from tests.test_server import FakeProvider, make_client, make_manager

    write_settings(tmp_path, {
        "presets": {"only": {"title": "Only", "orchestrator": {"tools": ["bash"]}}}
    })
    with make_client(make_manager(tmp_path, FakeProvider([]))) as client:
        first = client.get("/api/kernel/plugins/tool.bash").json()
        assert "only" in [u["id"] for u in first["used_by"]]
        assert all(u["href"].startswith("#/config/") for u in first["used_by"])

        write_settings(tmp_path, {
            "presets": {"only": {"title": "Only", "orchestrator": {"tools": ["read"]}}}
        })
        second = client.get("/api/kernel/plugins/tool.bash").json()
        assert "only" not in [u["id"] for u in second["used_by"]]
        assert "only" in [
            u["id"] for u in client.get("/api/kernel/plugins/tool.read").json()["used_by"]
        ]

        # The inventory carries it too, so a card can show a count without a
        # fetch -- and it is still one index build for the whole payload.
        inventory = client.get("/api/kernel").json()
        assert all("used_by" in p for p in inventory["plugins"])


def test_a_broken_preset_costs_the_block_not_the_page(project, monkeypatch):
    monkeypatch.setattr(
        "quickcode.kernel.preset.load_presets",
        lambda cwd: (_ for _ in ()).throw(RuntimeError("boom")))
    registry = build_registry(project)
    assert registry.used_by("tool.bash") == []
    assert registry.plugin_json("tool.bash")["used_by"] == []
