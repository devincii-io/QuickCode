"""``http.scoped``: one handler mounted as ``/api{suffix}`` and
``/api/projects/{pid}{suffix}``, with nothing about the handler changed on the way.

Most of the API is mounted this way, so what it must not change is exactly what
a hand-written pair of routes would have kept: the parameters FastAPI reads, a
sync handler staying in the threadpool, and the default project being read on
each request rather than once at startup.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from quickcode.server.http import DEFAULT, PROJECT, read_json, scoped
from quickcode.server.manager import ConversationManager
from quickcode.server.projects import ProjectHub, project_id
from tests.test_server import FakeProvider, make_manager


def where(manager: ConversationManager, name: str, loud: bool = False) -> dict:
    return {"cwd": str(manager.cwd), "name": name.upper() if loud else name}


async def echo(manager: ConversationManager, request: Request) -> dict:
    return {"cwd": str(manager.cwd), "body": await read_json(request)}


def _hub(tmp_path: Path) -> ProjectHub:
    first = tmp_path / "first"
    first.mkdir()
    return ProjectHub.from_manager(make_manager(first, FakeProvider([])))


def test_both_shapes_answer_from_one_handler(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    app = FastAPI()
    scoped(app, hub, "GET", "/where/{name}", where)
    scoped(app, hub, "POST", "/echo", echo)
    pid = hub.default_id
    with TestClient(app) as client:
        assert client.get("/api/where/x?loud=true").json() == {
            "cwd": str(hub.default.cwd), "name": "X",
        }
        assert client.get(f"/api/projects/{pid}/where/y").json() == {
            "cwd": str(hub.default.cwd), "name": "y",
        }
        assert client.post(f"/api/projects/{pid}/echo", json={"a": 1}).json()["body"] == {"a": 1}
        missing = client.get("/api/projects/nope/where/x")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "unknown project: nope"


def test_the_default_shape_reads_the_hub_s_default_on_each_request(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    app = FastAPI()
    scoped(app, hub, "GET", "/where/{name}", where)
    second = tmp_path / "second"
    second.mkdir()
    with TestClient(app) as client:
        assert client.get("/api/where/x").json()["cwd"] == str(tmp_path / "first")
        hub.adopt(make_manager(second, FakeProvider([])), default=True)
        assert client.get("/api/where/x").json()["cwd"] == str(second)
        assert client.get(f"/api/projects/{project_id(second)}/where/x").status_code == 200


def test_routes_keep_the_handler_s_name_order_and_sync_or_async_kind(tmp_path: Path) -> None:
    """A sync handler must stay sync: FastAPI runs it in the threadpool, and one
    turned into a coroutine would do its file IO on the event loop."""
    hub = _hub(tmp_path)
    app = FastAPI()
    scoped(app, hub, "GET", "/where/{name}", where, shapes=(PROJECT, DEFAULT))
    scoped(app, hub, "POST", "/echo", echo)
    routes = [r for r in app.routes if getattr(r, "name", "") in {
        "where", "project_where", "echo", "project_echo",
    }]
    assert [(r.path, r.name) for r in routes] == [
        ("/api/projects/{pid}/where/{name}", "project_where"),
        ("/api/where/{name}", "where"),
        ("/api/echo", "echo"),
        ("/api/projects/{pid}/echo", "project_echo"),
    ]
    kinds = {r.name: inspect.iscoroutinefunction(r.endpoint) for r in routes}
    assert kinds == {"where": False, "project_where": False, "echo": True, "project_echo": True}
