"""The permission-profile routes, and what a session does with what they write.

``tests/test_profiles.py`` owns the model -- what a profile means and what the
trust gate does to one. This file owns the four routes and the two places their
answer has to show up: a session that is opened afterwards, and a session that
is already running.

Three groups. The routes and every way of getting a 400 out of them; the
posture a session actually starts with, including on a resume; and the security
half -- an untrusted project may narrow what the agent does through any of
these routes and may not widen it through any of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quickcode.core.permissions import Mode
from quickcode.kernel import state as state_store
from quickcode.security import trust
from tests.test_server import FakeProvider, make_client, make_manager

BUILTINS = {"readonly", "git", "survey", "build"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose trust store and user-scope settings both live in tmp.

    These routes write to both, so a test that skipped this would edit the
    developer's own ``~/.quickcode/settings.json``.
    """
    user = tmp_path / "userconfig"
    user.mkdir()
    monkeypatch.setattr(trust, "CONFIG_DIR", user)
    monkeypatch.setattr(state_store, "CONFIG_DIR", user)
    root = tmp_path / "proj"
    (root / ".quickcode").mkdir(parents=True)
    return root


def _client(root: Path, provider=None):
    return make_client(make_manager(root, provider or FakeProvider([])))


def _manager_and_client(root: Path, provider=None):
    manager = make_manager(root, provider or FakeProvider([]))
    return manager, make_client(manager)


def _project_settings(root: Path, data: dict) -> None:
    (root / ".quickcode" / "settings.json").write_text(
        json.dumps(data), encoding="utf-8",
    )


def _read_project_settings(root: Path) -> dict:
    return json.loads((root / ".quickcode" / "settings.json").read_text("utf-8"))


def _by_id(payload: dict) -> dict:
    return {p["id"]: p for p in payload["profiles"]}


def _grant(root: Path) -> None:
    """Trust the project, the way the trust route does."""
    trust.default_store().grant(root)


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

def test_get_lists_the_builtins_with_nothing_selected(project):
    with _client(project) as client:
        payload = client.get("/api/profiles").json()

    found = _by_id(payload)
    assert BUILTINS <= set(found)
    assert all(found[i]["layer"] == "default" for i in BUILTINS)
    assert all(found[i]["builtin"] is True for i in BUILTINS)
    # No profile is the default and a real answer: a session with none behaves
    # exactly as it did before profiles existed.
    assert payload["active"] == ""
    assert payload["problems"] == []
    # A project nobody has vouched for yet, which is what a fresh clone is.
    assert payload["trusted"] is False
    # The two halves the picker needs to grey a row before it is clicked.
    assert found["git"]["widens"] is True
    assert found["readonly"]["widens"] is False


def test_post_writes_a_user_profile_and_answers_with_the_whole_list(project):
    body = {
        "id": "git-and-tests", "title": "Git and tests",
        "description": "The two things I approve forty times an hour.",
        "mode": "ask", "allow": ["bash(git **)", "bash(pytest**)"],
        "ask": ["bash(git push**)"], "deny": ["web_fetch"], "scope": "user",
    }
    with _client(project) as client:
        answer = client.post("/api/profiles", json=body)
        assert answer.status_code == 200
        payload = answer.json()
        assert payload["saved"] == "git-and-tests"
        assert payload["scope"] == "user"

        # The write answers with the list, so the page never reconstructs it.
        made = _by_id(payload)["git-and-tests"]
        assert made["layer"] == "user"
        assert made["builtin"] is False
        assert made["allow"] == ["bash(git **)", "bash(pytest**)"]
        assert made["deny"] == ["web_fetch"]
        # And a second read agrees, which is the part that proves it was
        # written rather than echoed.
        assert "git-and-tests" in _by_id(client.get("/api/profiles").json())


def test_post_with_project_scope_writes_the_projects_own_file(project):
    body = {"id": "no-network", "title": "No network", "mode": "ask",
            "deny": ["web_fetch", "web_search"], "scope": "project"}
    with _client(project) as client:
        payload = client.post("/api/profiles", json=body).json()

    assert payload["scope"] == "project"
    assert _by_id(payload)["no-network"]["layer"] == "project"
    assert "no-network" in _read_project_settings(project)["profiles"]


def test_delete_removes_it_and_404s_when_there_is_nothing_to_remove(project):
    with _client(project) as client:
        client.post("/api/profiles", json={"id": "mine", "title": "Mine",
                                           "mode": "ask", "scope": "user"})
        gone = client.delete("/api/profiles/mine?scope=user")
        assert gone.status_code == 200
        assert "mine" not in _by_id(gone.json())

        again = client.delete("/api/profiles/mine?scope=user")
        assert again.status_code == 404
        assert "mine" in again.json()["detail"]


def test_delete_of_a_builtin_is_404_because_there_is_no_file_to_remove_it_from(
        project):
    with _client(project) as client:
        answer = client.delete("/api/profiles/readonly?scope=user")
    assert answer.status_code == 404


def test_delete_rejects_a_scope_that_is_neither_file(project):
    with _client(project) as client:
        answer = client.delete("/api/profiles/mine?scope=everywhere")
    assert answer.status_code == 400
    assert "scope" in answer.json()["detail"]


# ---- validation: each failure names the thing that failed ------------------

@pytest.mark.parametrize("bad_id", ["", "   ", "../evil", "has space", "-lead"])
def test_post_rejects_an_id_a_settings_key_or_a_url_could_not_carry(
        project, bad_id):
    with _client(project) as client:
        answer = client.post("/api/profiles",
                             json={"id": bad_id, "title": "x", "mode": "ask"})
    assert answer.status_code == 400
    assert "profile id" in answer.json()["detail"]


def test_post_rejects_an_unknown_mode_and_says_which_ones_exist(project):
    with _client(project) as client:
        answer = client.post("/api/profiles",
                             json={"id": "x", "title": "x", "mode": "godmode"})
    assert answer.status_code == 400
    detail = answer.json()["detail"]
    assert "mode: godmode" in detail and "auto-edit" in detail


def test_post_rejects_a_rule_the_engine_could_never_match(project):
    with _client(project) as client:
        answer = client.post("/api/profiles", json={
            "id": "x", "title": "x", "mode": "ask",
            "allow": ["bash(git **)", "bash(git **"],
        })
    assert answer.status_code == 400
    # The user's own text, quoted back: a message that paraphrased it would
    # leave them hunting for which of ten lines it meant.
    assert "allow: bash(git **" in answer.json()["detail"]


def test_post_rejects_a_body_that_is_not_an_object(project):
    with _client(project) as client:
        answer = client.post("/api/profiles", json=["readonly"])
    assert answer.status_code == 400
    assert "JSON object" in answer.json()["detail"]


def test_active_rejects_a_body_without_an_id(project):
    with _client(project) as client:
        answer = client.post("/api/profiles/active", json={"profile": "git"})
    assert answer.status_code == 400
    assert "'id'" in answer.json()["detail"]


def test_active_rejects_a_profile_that_does_not_exist(project):
    with _client(project) as client:
        answer = client.post("/api/profiles/active", json={"id": "nope"})
    assert answer.status_code == 400
    assert "nope" in answer.json()["detail"]


# ---- shadowing a built-in --------------------------------------------------

def test_post_over_a_builtin_is_refused_until_it_is_asked_for_twice(project):
    """409, not 400 and not a silent write.

    The request is valid; it just does something the user has to have meant.
    Saving over a built-in's id does not edit the built-in -- there is no file
    to edit -- it writes a copy that hides it everywhere under the same title,
    which is exactly the change nobody notices afterwards.
    """
    body = {"id": "readonly", "title": "Read only", "mode": "yolo",
            "allow": ["bash(**)"], "scope": "user"}
    with _client(project) as client:
        refused = client.post("/api/profiles", json=body)
        assert refused.status_code == 409
        assert "built-in" in refused.json()["detail"]
        # Refused means nothing was written.
        assert _by_id(client.get("/api/profiles").json())["readonly"]["mode"] == "plan"

        payload = client.post("/api/profiles",
                              json={**body, "shadow": True}).json()
        shadow = _by_id(payload)["readonly"]
        assert shadow["layer"] == "user" and shadow["builtin"] is False

        # Reversible, which is the other half of why 409 rather than 403: the
        # shadow is a file, and deleting it brings the built-in back.
        restored = client.delete("/api/profiles/readonly?scope=user").json()
        assert _by_id(restored)["readonly"]["layer"] == "default"
        assert _by_id(restored)["readonly"]["mode"] == "plan"


# ---------------------------------------------------------------------------
# what a session runs
# ---------------------------------------------------------------------------

async def test_switching_reaches_a_session_that_is_already_open(project):
    """The whole point of the pill: no reopening, no turn boundary."""
    _grant(project)
    manager, client = _manager_and_client(project)
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.conversations[conv_id]
        assert conv.agent.mode == Mode.ask
        assert "bash(git **)" not in conv.agent.permissions.rules.allow

        answer = client.post("/api/profiles/active", json={"id": "git"}).json()
        assert [a["conv_id"] for a in answer["applied_to"]] == [conv_id]

        # The engine the *running* conversation holds, not a fresh one.
        assert "bash(git **)" in conv.agent.permissions.rules.allow
        assert "bash(git push**)" in conv.agent.permissions.rules.ask
        assert conv.profile_id == "git"

        # And clearing puts it back without reopening either.
        client.post("/api/profiles/active", json={"id": ""})
        assert "bash(git **)" not in conv.agent.permissions.rules.allow
        assert conv.profile_id == ""
    await manager.close()


async def test_a_switch_keeps_the_always_allows_granted_in_this_session(project):
    """The union decision, at the one boundary that could quietly undo it.

    An "always allow" answered mid-session is appended to the live engine and
    written to ``settings.local.json`` -- whose allow list is gated on project
    trust like every other. So in an untrusted project the grant lives only in
    the engine, and rebuilding the rule set from a profile would revoke it.
    Twice over, because the second switch is where a naive carry loses it.
    """
    manager, client = _manager_and_client(project)
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.conversations[conv_id]
        conv.agent.permissions.rules.persist_allow(project, "bash(npm test**)")

        client.post("/api/profiles/active", json={"id": "survey"})
        assert "bash(npm test**)" in conv.agent.permissions.rules.allow
        client.post("/api/profiles/active", json={"id": "readonly"})
        assert "bash(npm test**)" in conv.agent.permissions.rules.allow
        client.post("/api/profiles/active", json={"id": ""})
        assert "bash(npm test**)" in conv.agent.permissions.rules.allow
    await manager.close()


class _Sink:
    """A websocket stand-in: collects everything a conversation broadcasts."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, text: str) -> None:
        self.events.append(json.loads(text))


def _reckless_project(project) -> None:
    _grant(project)
    _project_settings(project, {"profiles": {"reckless": {
        "title": "Reckless", "mode": "yolo", "allow": ["bash(**)"],
    }}})
    _grant(project)                      # the write moved the trust hash


async def test_a_switch_never_climbs_into_yolo_and_says_so_out_loud(project):
    """A posture is a posture, not a second way to arm yolo.

    The refusal is half the behaviour. Downgrading in silence is what made a
    profile look broken: the user picked it, the pill stayed on ``ask``, and
    nothing in the transcript connected the two.
    """
    _reckless_project(project)
    manager, client = _manager_and_client(project)
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.conversations[conv_id]
        sink = _Sink()
        conv.clients.add(sink)
        answer = client.post("/api/profiles/active",
                             json={"id": "reckless"}).json()
        assert answer["applied_to"][0]["mode"] != "yolo"
        assert conv.agent.mode != Mode.yolo
        said = [e["text"] for e in sink.events if e.get("type") == "system_note"]
        assert any("yolo" in t and "Settings" in t for t in said), said
    await manager.close()


async def test_a_profile_may_ask_for_yolo_once_the_app_has_armed_it(project):
    """Arming is what makes yolo reachable; the profile then simply applies."""
    _reckless_project(project)
    manager, client = _manager_and_client(project)
    manager.config.allow_yolo = True
    with client:
        conv_id = client.post("/api/conversations", json={}).json()["conv_id"]
        conv = manager.conversations[conv_id]
        answer = client.post("/api/profiles/active",
                             json={"id": "reckless"}).json()
        assert answer["applied_to"][0]["mode"] == "yolo"
        assert conv.agent.mode == Mode.yolo
    await manager.close()


async def test_a_new_session_starts_in_the_profiles_mode_with_its_rules(project):
    _project_settings(project, {"active_profile": "readonly"})
    manager = make_manager(project, FakeProvider([]))
    conv = manager.open()
    try:
        assert conv.agent.mode == Mode.plan
        assert "write" in conv.agent.permissions.rules.deny
        assert conv.profile_id == "readonly"
    finally:
        await manager.close()


async def test_a_resumed_session_gets_the_profile_too(project):
    """The judgement call, and the reason it went this way.

    The tempting rule is that a profile's mode is a *starting* mode, so a
    resumed session should keep the mode it already had. There is nothing to
    keep: no per-session mode is written to disk, so "the mode it had" would in
    fact be the install's default. Honouring the profile only on a fresh open
    would therefore make reopening a Read-only session hand it back in ``ask``,
    which is *wider* than the session being resumed -- the exact failure the
    rule is trying to prevent, arrived at from the other side.
    """
    _project_settings(project, {"active_profile": "readonly"})
    first = make_manager(project, FakeProvider([]))
    conv_id = first.open().conv_id
    await first.close()

    second = make_manager(project, FakeProvider([]))
    resumed = second.open(conv_id)
    try:
        assert resumed.conv_id == conv_id
        assert resumed.agent.mode == Mode.plan
        assert "write" in resumed.agent.permissions.rules.deny
    finally:
        await second.close()


async def test_an_explicit_launch_mode_outranks_the_profiles(project):
    """``--mode`` is the operator saying it now; a profile is a file."""
    from quickcode.config import Config
    from quickcode.server.manager import ConversationManager
    from tests.test_server import make_env

    _project_settings(project, {"active_profile": "readonly"})
    cfg = Config()
    cfg.last_model = "test/model"
    manager = ConversationManager(
        cwd=project, config=cfg, env=make_env(project),
        provider=FakeProvider([]), default_mode="ask",
    )
    conv = manager.open()
    try:
        assert conv.agent.mode == Mode.ask
        # The rules are not the flag's business, so they still apply.
        assert "write" in conv.agent.permissions.rules.deny
    finally:
        await manager.close()


async def test_the_profiles_rules_are_added_to_the_projects_own(project):
    """Merged, never substituted: an accrued "always allow" survives a switch."""
    _project_settings(project, {
        "permissions": {"allow": ["bash(ls **)"]},
        "active_profile": "survey",
    })
    _grant(project)
    manager = make_manager(project, FakeProvider([]))
    conv = manager.open()
    try:
        rules = conv.agent.permissions.rules
        assert "bash(ls **)" in rules.allow          # the project's own
        assert "read(**)" in rules.deny              # the profile's
    finally:
        await manager.close()


# ---------------------------------------------------------------------------
# an untrusted project may narrow and may not widen -- through any route
# ---------------------------------------------------------------------------

def test_an_untrusted_projects_profile_loses_its_allow_rules_and_says_so(project):
    _project_settings(project, {"profiles": {"generous": {
        "title": "Generous", "mode": "auto-edit",
        "allow": ["bash(**)"], "deny": ["web_fetch"],
    }}})
    with _client(project) as client:
        payload = client.get("/api/profiles").json()

    generous = _by_id(payload)["generous"]
    assert generous["allow"] == []
    assert generous["mode"] == "ask"
    assert set(generous["refused"]) == {"allow", "mode"}
    # The narrowing half is untouched, because narrowing needs nobody's consent.
    assert generous["deny"] == ["web_fetch"]
    # And it is findable, not just quietly reduced.
    assert any(p["code"] == "profile_refused" for p in payload["problems"])


def test_selecting_a_widening_profile_in_an_untrusted_project_is_refused(project):
    """Not written and then ignored -- refused, while there is someone to tell.

    The selection lives in the project's settings file, where nothing can tell
    it apart from the same line committed by the repository. So the route
    cannot write it and call it applied.
    """
    with _client(project) as client:
        answer = client.post("/api/profiles/active", json={"id": "git"})
        assert answer.status_code == 409
        detail = answer.json()["detail"]
        assert "trust" in detail.lower()
        # Nothing was written, so nothing arms itself later either.
        assert client.get("/api/profiles").json()["active"] == ""

        # A profile that only narrows needs no consent and goes through.
        assert client.post("/api/profiles/active",
                           json={"id": "survey"}).status_code == 200
        assert client.get("/api/profiles").json()["active"] == "survey"


def test_the_same_selection_works_once_the_project_is_trusted(project):
    _grant(project)
    with _client(project) as client:
        assert client.post("/api/profiles/active",
                           json={"id": "git"}).status_code == 200
        assert client.get("/api/profiles").json()["active"] == "git"


def test_posting_an_allow_rule_at_project_scope_does_not_grant_itself_trust(
        project):
    """The bypass this closes: save a widening profile, be trusted for it.

    Writing the file is allowed -- it is the user's own project and the editor
    has to be able to write there. What is not allowed is the write counting as
    the grant, so the rule lands on disk and stays inert until someone answers
    the trust prompt.
    """
    with _client(project) as client:
        payload = client.post("/api/profiles", json={
            "id": "wide", "title": "Wide", "mode": "auto-edit",
            "allow": ["bash(**)"], "scope": "project",
        }).json()

        assert "wide" in _read_project_settings(project)["profiles"]
        assert payload["trusted"] is False
        wide = _by_id(payload)["wide"]
        assert wide["allow"] == [] and "allow" in wide["refused"]

    assert trust.is_trusted(project) is False


async def test_an_untrusted_projects_profile_cannot_widen_a_session(project):
    """The end of the line: whatever the file says, the engine does not get it."""
    _project_settings(project, {"profiles": {"generous": {
        "title": "Generous", "mode": "auto-edit", "allow": ["bash(**)"],
    }}, "active_profile": "generous"})
    manager = make_manager(project, FakeProvider([]))
    conv = manager.open()
    try:
        assert "bash(**)" not in conv.agent.permissions.rules.allow
        assert conv.agent.mode != Mode.auto_edit
    finally:
        await manager.close()
