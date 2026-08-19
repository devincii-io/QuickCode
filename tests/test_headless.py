"""Headless (`quickcode -p`) runs leave the same traceable session log a UI run
does — and stay resumable, and stay readable when the turn is cut short.

Everything here drives the real CLI entry point with a scripted fake provider:
no live model call is made, and none is needed to prove what the log contains.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from quickcode import cli
from quickcode.config import Config
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone, Usage
from quickcode.kernel.composition import RuntimeLimits
from quickcode.session.store import SessionStore
from tests.test_server import FakeProvider


async def _refuse():
    raise RuntimeError("no catalog reachable")


class CallbackProvider(FakeProvider):
    """A scripted provider that runs a hook before each response.

    The hook is how a test reaches into the middle of a turn — to cancel it,
    or to blow it up — which is the only way to exercise what a half-finished
    headless run leaves on disk.
    """

    def __init__(self, scripts, before=None):
        super().__init__(scripts)
        self.before = before

    async def stream_chat(self, req):
        if self.before is not None:
            self.before(req)
        async for ev in super().stream_chat(req):
            yield ev


def _install(monkeypatch, provider, built=None):
    """Point the CLI at a fake provider and a throwaway config."""
    def load(cls=None, path=None):
        cfg = Config()
        cfg.last_model = "test/model"
        return cfg

    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: load()))
    # Patched where it is defined, not where it is used: the CLI imports the
    # provider inside _build_agent so that starting the app does not drag the
    # OpenAI SDK in before the window exists, and a module-attribute patch on
    # `cli` would break the moment that import moved.
    monkeypatch.setattr(
        "quickcode.providers.openai_compat.OpenAICompatProvider",
        lambda *a, **k: provider,
    )
    if built is not None:
        real = cli._build_agent

        def spy(args):
            out = real(args)
            built["agent"], built["store"] = out[0], out[3]
            # Handed to the provider before the turn starts, so a script can
            # reach back and interrupt the agent mid-stream.
            if hasattr(provider, "agent"):
                provider.agent = out[0]
            return out

        monkeypatch.setattr(cli, "_build_agent", spy)


def _headless(tmp_path, *extra):
    return ["--print", "--cwd", str(tmp_path), "--mode", "ask", *extra]


def _events(tmp_path, conv_id=None):
    conv_id = conv_id or SessionStore.most_recent(tmp_path)
    return SessionStore(tmp_path, conv_id).load_events()


def _read_script(target):
    """Two rounds: read a file, then answer."""
    return [
        [
            ToolCallEnd(id="c1", name="read",
                        arguments=json.dumps({"file_path": str(target)})),
            TurnDone("tool_calls"),
        ],
        [TextDelta("the file says hello"),
         Usage(input_tokens=12, output_tokens=4), TurnDone("stop")],
    ]


def test_headless_turn_writes_a_full_session_log(tmp_path, monkeypatch, capsys):
    """The defect: a `-p` run used to leave a log holding only its meta line."""
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    _install(monkeypatch, FakeProvider(_read_script(target)))

    cli.main(_headless(tmp_path, "what does note.txt say?"))
    assert capsys.readouterr().out.strip() == "the file says hello"

    events = _events(tmp_path)
    by_type = {e["type"] for e in events}
    assert {"system_prompt", "user_message", "tool_call",
            "tool_result", "assistant_message", "usage"} <= by_type

    user = [e for e in events if e["type"] == "user_message"]
    assert [e["text"] for e in user] == ["what does note.txt say?"]
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "read"
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] is False and "hello" in result["content"]
    assert [e["text"] for e in events if e["type"] == "assistant_message"] == [
        "the file says hello"]

    # Same stamping the web path applies: strictly increasing seqs, and the
    # whole turn attributed to turn 1 (the system prompt precedes it).
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs))
    assert {e["turn"] for e in events if e["type"] != "system_prompt"} == {1}


def test_headless_turn_persists_messages_for_continue(tmp_path, monkeypatch, capsys):
    """`--continue` resumes a real conversation, not an empty one."""
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    _install(monkeypatch, FakeProvider(_read_script(target)))
    cli.main(_headless(tmp_path, "what does note.txt say?"))
    capsys.readouterr()

    conv_id = SessionStore.most_recent(tmp_path)
    messages = SessionStore(tmp_path, conv_id).load_messages()
    roles = [m.role for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "note.txt" in messages[0].content

    # A second headless run with --continue reopens that same session and sends
    # the prior turn back to the model.
    second = FakeProvider([[TextDelta("still hello"), TurnDone("stop")]])
    _install(monkeypatch, second)
    cli.main(_headless(tmp_path, "--continue", "and again?"))
    assert capsys.readouterr().out.strip() == "still hello"

    assert SessionStore.most_recent(tmp_path) == conv_id  # no second session
    sent = [m.content for m in second.requests[0].messages]
    assert any("note.txt" in (c or "") for c in sent)
    assert any("the file says hello" == c for c in sent)

    after = SessionStore(tmp_path, conv_id).load_messages()
    assert [m.role for m in after] == [*roles, "user", "assistant"]
    assert after[-1].content == "still hello"


class CancelMidStreamProvider:
    """Reads a file, then starts answering and is interrupted mid-sentence.

    The second round never reaches ``TurnDone``, so nothing in the normal path
    assembles the text that had already streamed — which is exactly the case
    the headless driver has to close out itself.
    """

    def __init__(self, target):
        self.target = target
        self.agent = None
        self.round = 0

    async def stream_chat(self, req):
        self.round += 1
        if self.round == 1:
            yield ToolCallEnd(id="c1", name="read",
                              arguments=json.dumps({"file_path": str(self.target)}))
            yield TurnDone("tool_calls")
            return
        yield TextDelta("half an ans")
        self.agent.cancel()  # ctrl-c lands here
        yield TextDelta("wer")
        yield TurnDone("stop")

    async def list_models(self):
        return []


def test_interrupted_headless_turn_leaves_a_readable_log(tmp_path, monkeypatch, capsys):
    """Cancelled mid-stream: the log parses, and keeps what had been said."""
    target = tmp_path / "x.txt"
    target.write_text("body", encoding="utf-8")
    built: dict = {}
    provider = CancelMidStreamProvider(target)
    _install(monkeypatch, provider, built)

    cli.main(_headless(tmp_path, "read it"))
    capsys.readouterr()

    path = SessionStore(tmp_path, SessionStore.most_recent(tmp_path)).path
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for line in lines:  # every record whole — no half-written line
        json.loads(line)
    events = _events(tmp_path)
    assert [e["text"] for e in events if e["type"] == "user_message"] == ["read it"]
    assert any(e["type"] == "tool_result" for e in events)
    # The text that had streamed before the interrupt is in the log, marked as
    # what it is, rather than dropped with the unfinished round.
    cut = [e for e in events if e["type"] == "assistant_message"][-1]
    assert cut["text"] == "half an ans"
    assert cut["finish_reason"] == "interrupted"
    # The turn's messages are persisted, so the session is still resumable.
    assert [m.role for m in SessionStore(tmp_path, path.stem).load_messages()] == [
        "user", "assistant", "tool"]


def test_failing_headless_turn_leaves_a_readable_log(tmp_path, monkeypatch, capsys):
    """A turn that raises records what it got, plus the error, and re-raises."""
    def boom(req):
        if len(provider.requests) >= 1:
            raise RuntimeError("provider exploded")

    provider = CallbackProvider(
        [[ToolCallEnd(id="c1", name="read",
                      arguments=json.dumps({"file_path": str(tmp_path / "x.txt")})),
          TurnDone("tool_calls")]],
        before=boom,
    )
    (tmp_path / "x.txt").write_text("body", encoding="utf-8")
    _install(monkeypatch, provider)

    with pytest.raises(RuntimeError):
        cli.main(_headless(tmp_path, "read it"))
    capsys.readouterr()

    path = SessionStore(tmp_path, SessionStore.most_recent(tmp_path)).path
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)
    events = _events(tmp_path)
    assert [e["text"] for e in events if e["type"] == "user_message"] == ["read it"]
    assert any(e["type"] == "tool_result" for e in events)
    errors = [e["message"] for e in events if e["type"] == "error"]
    assert errors and "provider exploded" in errors[-1]
    # The session is not "empty" — it has a transcript and can be listed and
    # resumed like any other.
    store = SessionStore(tmp_path, path.stem)
    assert store.is_empty() is False
    assert store.title().startswith("read it")


def test_starting_the_app_does_not_import_the_openai_sdk():
    """Importing the entry points must not pull in the provider SDK.

    The OpenAI SDK is roughly two thirds of this application's import cost —
    it brings in the Pydantic model trees for Assistants, graders, evals and
    batches, none of which QuickCode uses. Paying that before the window
    exists is what made a cold start look like nothing was happening. It is
    loaded on the first request instead, where it hides behind model latency.

    A fresh interpreter, because the in-process module table is long since
    polluted by the rest of the suite.
    """
    code = (
        "import sys; import quickcode.cli, quickcode.webapp; "
        "print('openai' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", (
        "something re-introduced a module-level `import openai`; import it "
        "inside the function that needs it instead"
    )


async def test_the_headless_run_learns_its_context_window_without_delaying_the_turn():
    """The end-of-turn compaction check needs a context window to compare against.

    `-p` built its agent with `context_length=None`, so the check could never
    fire and a `--continue` chain grew without bound. The catalog is fetched
    alongside the turn rather than before it: a one-shot CLI must not pay a
    network round trip before it starts working.
    """
    provider = FakeProvider([])
    agent = SimpleNamespace(
        context_length=None, provider=provider, model="test/model",
        limits=RuntimeLimits(),
    )
    await cli._warm_context_length(agent)
    assert agent.context_length == 100_000


async def test_a_context_window_is_not_fetched_when_compaction_is_switched_off():
    provider = FakeProvider([])
    provider.list_models = _refuse
    agent = SimpleNamespace(
        context_length=None, provider=provider, model="test/model",
        limits=RuntimeLimits(compaction_enabled=False),
    )
    await cli._warm_context_length(agent)          # must not raise
    assert agent.context_length is None


async def test_a_provider_with_no_catalog_leaves_the_meter_exactly_as_it_was():
    provider = FakeProvider([])
    provider.list_models = _refuse
    agent = SimpleNamespace(
        context_length=None, provider=provider, model="test/model",
        limits=RuntimeLimits(),
    )
    await cli._warm_context_length(agent)          # a dead catalog is not an error
    assert agent.context_length is None


def test_a_headless_delegation_is_bracketed_in_the_log(tmp_path, monkeypatch, capsys):
    """A ``-p`` run logged a subagent starting and nothing at all after it.

    Every headless delegation blocks -- there is nothing here to own a
    detached job -- so the blocking shape was the one shape whose ending was
    never written down, in the one place with no live UI to compensate.
    """
    spawn = json.dumps({
        "description": "look around", "prompt": "look", "agent_type": "explore",
    })

    class DelegatingProvider(FakeProvider):
        async def stream_chat(self, req):
            system = next((m.content for m in req.messages if m.role == "system"), "")
            if "QuickCode subagent" in (system or ""):
                yield TextDelta("nothing to report")
                yield TurnDone("stop")
                return
            async for ev in super().stream_chat(req):
                yield ev

    _install(monkeypatch, DelegatingProvider([
        [ToolCallEnd(id="a1", name="agent", arguments=spawn), TurnDone("tool_calls")],
        [TextDelta("the child found nothing"), TurnDone("stop")],
    ]))

    cli.main(_headless(tmp_path, "delegate this"))
    capsys.readouterr()

    events = _events(tmp_path)
    spawned = [e for e in events if e["type"] == "agent_spawned"]
    done = [e for e in events if e["type"] == "agent_done"]
    assert [e["agent_id"] for e in spawned] == [e["agent_id"] for e in done]
    assert done[0]["status"] == "done"
