"""The terminal panel: one interactive shell per socket, and nothing else.

The panel is the one surface in QuickCode where an unrestricted shell is the
*point* — it is the user typing, so the permission engine has no business in
it. That makes two properties load-bearing rather than nice to have, and both
are asserted here: the socket is behind the same auth as every other socket,
and no code the model can reach knows the route exists.

The rest is lifecycle. A shell that is spawned but not killed is a `bash.exe`
in the task list after the window is gone, so every way the session can end —
the socket closing, the shell exiting, the project being closed — is pinned.

These drive a real pseudo-terminal, but not a real *shell*: ``shell_argv`` is
swapped for a small Python program that behaves like one (prints a banner with
its cwd, echoes each line, exits on ``quit``). A login shell would make every
assertion depend on the developer's ``.bashrc``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from quickcode.pty import registry
from quickcode.pty.interactive import InteractivePty, interactive_shell_argv
from quickcode.server import terminal
from quickcode.server.app import create_app
from quickcode.server.projects import project_id
from tests.conftest import wait_until
from tests.test_projects import make_hub
from tests.test_server import FakeProvider

# A shell for testing purposes: a banner naming its working directory, one
# echoed line per line typed, and a distinctive exit code so "the shell ended"
# is provable rather than inferred.
FAKE_SHELL = '''
import os, sys
sys.stdout.write("READY " + os.getcwd().replace("\\\\", "/") + "\\n")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    sys.stdout.write("echo:" + line + "\\n")
    sys.stdout.flush()
sys.exit(7)
'''


@pytest.fixture
def fake_shell(tmp_path_factory, monkeypatch):
    """Point ``terminal.shell_argv`` at the stand-in shell above."""
    script = tmp_path_factory.mktemp("fake-shell") / "shell.py"
    script.write_text(FAKE_SHELL, encoding="utf-8")
    argv = [sys.executable, "-u", str(script)]
    monkeypatch.setattr(terminal, "shell_argv", lambda: list(argv))
    return argv


@pytest.fixture(autouse=True)
def _no_terminal_outlives_a_test():
    yield
    registry.close_all()


def terminal_socket(client: TestClient, path: str, **kw):
    # TestClient's handshake carries Host: testserver; the local guard wants the
    # loopback host the app was configured with.
    headers = {"host": "127.0.0.1:8642", **kw.pop("headers", {})}
    return client.websocket_connect(path, headers=headers, **kw)


def read_until(ws, needle: str, limit: int = 400) -> str:
    """Accumulate ``output`` frames until ``needle`` shows up in them."""
    seen = ""
    for _ in range(limit):
        ev = ws.receive_json()
        if ev.get("type") == "output":
            seen += ev["data"]
            if needle in seen:
                return seen
        elif ev.get("type") == "exit":
            raise AssertionError(f"the shell exited before {needle!r} arrived: {seen!r}")
        elif ev.get("type") == "terminal_error":
            raise AssertionError(f"the shell would not start: {ev['message']}")
    raise AssertionError(f"never saw {needle!r}; got {seen!r}")


def app_for(tmp_path: Path, *dirs: Path):
    """A hub with one project per directory, and a client onto it."""
    provider = FakeProvider([])
    hub = make_hub(tmp_path / "reg", provider, dirs[0])
    for extra in dirs[1:]:
        asyncio.run(hub.open(extra))
    app = create_app(hub, host="127.0.0.1", port=8642, token="")
    return hub, TestClient(app, base_url="http://127.0.0.1:8642")


# ---------------------------------------------------------------- lifecycle


def test_a_terminal_socket_runs_a_shell_in_the_projects_own_directory(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/terminal") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "terminal_ready"
        assert Path(ready["cwd"]).resolve() == proj.resolve()
        banner = read_until(ws, "READY ")
        reported = banner.split("READY ", 1)[1].splitlines()[0].strip()
        assert Path(reported).resolve() == proj.resolve()


def test_what_the_client_types_reaches_the_shell_and_its_answer_comes_back(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/terminal") as ws:
        ws.receive_json()
        read_until(ws, "READY ")
        ws.send_json({"type": "input", "data": "hello there\r"})
        assert "echo:hello there" in read_until(ws, "echo:hello there")


def test_a_resize_is_accepted_and_the_shell_keeps_going(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/terminal") as ws:
        ws.receive_json()
        read_until(ws, "READY ")
        ws.send_json({"type": "resize", "rows": 44, "cols": 132})
        # Nothing is echoed for a resize, so the proof it did not break the
        # session is that the next keystroke still round-trips.
        ws.send_json({"type": "input", "data": "after resize\r"})
        assert "echo:after resize" in read_until(ws, "echo:after resize")


def test_a_malformed_frame_is_ignored_rather_than_killing_the_session(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/terminal") as ws:
        ws.receive_json()
        read_until(ws, "READY ")
        ws.send_text("not json at all")
        ws.send_json({"type": "resize", "rows": "wide", "cols": None})
        ws.send_json({"type": "input", "data": 12345})
        ws.send_json({"type": "input", "data": "still here\r"})
        assert "echo:still here" in read_until(ws, "echo:still here")


def test_the_shell_exiting_is_announced_and_the_socket_closes(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/terminal") as ws:
        ws.receive_json()
        read_until(ws, "READY ")
        ws.send_json({"type": "input", "data": "quit\r"})
        for _ in range(400):
            ev = ws.receive_json()
            if ev.get("type") == "exit":
                assert ev["code"] == 7
                break
        else:
            raise AssertionError("the shell exited and nobody said so")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_closing_the_socket_kills_the_shell(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client:
        with terminal_socket(client, "/ws/terminal") as ws:
            ws.receive_json()
            read_until(ws, "READY ")
            assert registry.count(proj) == 1
        # The shell is not the socket's to outlive: the handler's `finally`
        # kills the tree and drops it from the registry.
        assert wait_until(lambda: registry.count(proj) == 0)


def test_closing_a_project_kills_the_terminals_open_on_it(tmp_path, fake_shell):
    default = tmp_path / "default"
    other = tmp_path / "other"
    default.mkdir()
    other.mkdir()
    hub, client = app_for(tmp_path, default, other)
    pid = project_id(other)
    with client, terminal_socket(client, f"/ws/projects/{pid}/terminal") as ws:
        ws.receive_json()
        read_until(ws, "READY ")
        assert registry.count(other) == 1
        # `forget` refuses a project with live *conversations*; a terminal is
        # not one, so nothing else would ever end this shell.
        asyncio.run(hub.forget(pid))
        assert registry.count(other) == 0


# ------------------------------------------------------------------ scoping


def test_each_project_gets_its_own_shell_and_cannot_reach_another_ones(tmp_path, fake_shell):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _hub, client = app_for(tmp_path, first, second)
    second_id = project_id(second)
    with client:
        with terminal_socket(client, "/ws/terminal") as a:
            a.receive_json()
            banner_a = read_until(a, "READY ")
            with terminal_socket(client, f"/ws/projects/{second_id}/terminal") as b:
                ready_b = b.receive_json()
                banner_b = read_until(b, "READY ")
                assert Path(ready_b["cwd"]).resolve() == second.resolve()
                # Two sockets, two shells, two directories. There is no shared
                # terminal id in the protocol, so there is nothing for a second
                # client to attach *to* — the only way to a shell is to name a
                # project and get one of your own, in that project's directory.
                assert second.name in banner_b and second.name not in banner_a
                assert registry.count(first) == 1
                assert registry.count(second) == 1


def test_a_terminal_for_an_unknown_project_is_refused(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client, terminal_socket(client, "/ws/projects/nosuchproject/terminal") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4404
    assert registry.count() == 0


def test_a_terminal_socket_without_the_token_is_refused(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    provider = FakeProvider([])
    hub = make_hub(tmp_path / "reg", provider, proj)
    app = create_app(hub, host="127.0.0.1", port=8642, token="s3cret")
    client = TestClient(app, base_url="http://127.0.0.1:8642")
    with client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with terminal_socket(client, "/ws/terminal") as ws:
                ws.receive_json()
        assert excinfo.value.code == 4403
        # ...and the same socket with the token opens a shell.
        with terminal_socket(client, "/ws/terminal", subprotocols=["qcauth.s3cret"]) as ws:
            assert ws.receive_json()["type"] == "terminal_ready"
    assert wait_until(lambda: registry.count() == 0)


def test_a_terminal_socket_from_another_origin_is_refused(tmp_path, fake_shell):
    proj = tmp_path / "proj"
    proj.mkdir()
    _hub, client = app_for(tmp_path, proj)
    with client:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with terminal_socket(
                client, "/ws/terminal", headers={"origin": "http://evil.example"}
            ) as ws:
                ws.receive_json()
        assert excinfo.value.code == 4403
    assert registry.count() == 0


# ------------------------------------------------------- the model's reach


def test_nothing_the_model_can_call_knows_the_terminal_exists() -> None:
    """The panel is an ungated shell, so the model must have no door to it.

    Asserted structurally rather than behaviourally: the failure this guards
    against is somebody *adding* a door — a tool that opens the socket, or a
    conversation frame routed into it — and that is visible in the source long
    before it is visible in a test run.
    """
    root = Path(__file__).resolve().parents[1] / "quickcode"
    doors = ["ws/terminal", "InteractivePty", "serve_terminal", "server.terminal"]
    offenders = []
    for path in list((root / "tools").rglob("*.py")) + [root / "server" / "manager.py"]:
        text = path.read_text(encoding="utf-8")
        for door in doors:
            if door in text:
                offenders.append(f"{path.name}: {door}")
    assert not offenders, (
        "the terminal panel runs a shell with no permission gate because a "
        "human is typing into it; these reach it from code the model drives:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------- the shell


def test_the_panels_shell_is_interactive_where_the_agents_is_one_shot() -> None:
    """Same shell binary, opposite invocation — and that is the whole design.

    ``tools/bash.py`` wants a process that runs one command and exits, so it
    passes ``-c``. The panel *is* the session, so it must not.
    """
    argv = interactive_shell_argv()
    assert argv, "no shell was found at all"
    assert "-c" not in argv and "-lc" not in argv
    if argv[0].endswith(("bash", "bash.exe")):
        assert "-i" in argv


def test_the_terminal_environment_promises_a_colour_terminal() -> None:
    env = terminal._shell_env()
    # Without TERM a program in a pty assumes a dumb terminal and turns colour
    # off, which would leave the panel's ANSI renderer nothing to render.
    assert env["TERM"] == "xterm-256color"


# ------------------------------------------------------------------ outbox


async def test_a_flood_of_output_is_bounded_and_keeps_the_newest() -> None:
    """`yes` into a terminal must not become the server's memory problem."""
    box = terminal._Outbox()
    chunk = "x" * 4096
    for _ in range(terminal.MAX_PENDING_CHARS // len(chunk) + 40):
        box.push(chunk)
    box.push("THE-NEWEST")
    text = await box.drain()
    assert len(text) <= terminal.MAX_PENDING_CHARS + len(chunk) + len("THE-NEWEST")
    assert text.endswith("THE-NEWEST")
    assert box.dropped > 0


async def test_the_outbox_coalesces_a_burst_into_one_frame() -> None:
    box = terminal._Outbox()
    for i in range(50):
        box.push(f"{i},")
    assert (await box.drain()).startswith("0,1,2,")
    # Drained means drained: the next wait blocks until something new arrives.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(box.drain(), 0.05)


# ------------------------------------------------------------ the pty itself


def test_closing_an_interactive_pty_ends_the_process(tmp_path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    pty = InteractivePty([sys.executable, "-u", str(script)], cwd=str(tmp_path))
    pty.start(lambda _text: None, lambda _code: None)
    assert wait_until(lambda: pty.alive)
    pty.close()
    assert not pty.alive
    pty.close()  # idempotent: the socket and the project may both get here
