"""The Jobs tab's routes (``server/jobs_api.py``): list, tail, kill.

Real processes in a real conversation, and the real app driven over ASGI on the
test's own loop -- the loop the job table hands its events to, so what reaches
the session log and an attached window here is what reaches them in the app.

The claim that matters most is the negative one: a person watching a job's
output must not change what ``bash_output`` tells the model is new.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from quickcode.server.app import create_app
from quickcode.server.conversation import Client
from quickcode.server.projects import project_id
from quickcode.tools.bash_jobs import EXITED, KILLED, RUNNING, BashJobs
from tests.conftest import await_until
from tests.test_bash_background import gate_file, read, start
from tests.test_server import FakeProvider, make_manager


@pytest.fixture
async def served(tmp_path):
    manager = make_manager(tmp_path, FakeProvider([]))
    conv = manager.open()
    app = create_app(manager, host="127.0.0.1", port=8642, token="")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8642") as client:
        yield SimpleNamespace(
            manager=manager, conv=conv, client=client, ctx=conv.agent.ctx,
            base=f"/api/conversations/{conv.conv_id}/jobs",
            pid=project_id(manager.cwd),
        )
    await manager.close()


def table(s) -> BashJobs:
    return s.ctx.extra["bash_jobs"]


def small_table(s, **kw) -> BashJobs:
    """Swap the conversation's table for one with tighter bounds."""
    s.ctx.extra["bash_jobs"] = jobs = BashJobs(on_event=s.conv.on_bash_job, **kw)
    return jobs


def logged(s, kind: str) -> list[dict]:
    return [e for e in s.conv.store.load_events() if e["type"] == kind]


async def tail(s, job_id: str, **params) -> dict:
    res = await s.client.get(f"{s.base}/{job_id}/output", params=params)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


async def test_the_list_says_what_each_job_is_and_how_it_stands(served, tmp_path):
    s = served
    go, wait_for_go = gate_file(tmp_path)
    await start(s.ctx, "echo done; exit 3", "Finish at once")
    await start(s.ctx, f"echo waiting; {wait_for_go}")
    first = table(s).get("bash_1")
    assert await await_until(lambda: not first.running)

    res = await s.client.get(s.base)

    assert res.status_code == 200
    body = res.json()
    rows = {r["id"]: r for r in body["jobs"]}
    assert set(rows) == {"bash_1", "bash_2"}
    assert body["running"] == 1 and body["max_running"] == table(s).max_running
    done, live = rows["bash_1"], rows["bash_2"]
    assert done["status"] == EXITED and done["exit_code"] == 3
    assert done["description"] == "Finish at once" and done["label"] == "Finish at once"
    assert done["command"] == "echo done; exit 3" and not done["command_truncated"]
    assert done["ended"] >= done["started"] > 0
    assert done["bytes"] == len(b"done\n") and done["dropped"] == 0
    assert done["unread"] == done["bytes"]
    assert live["status"] == RUNNING and live["ended"] is None and live["exit_code"] is None
    assert live["killed_by"] == ""

    # The project shape is the same handler over the same table.
    scoped = await s.client.get(f"/api/projects/{s.pid}/conversations/{s.conv.conv_id}/jobs")
    assert [r["id"] for r in scoped.json()["jobs"]] == ["bash_1", "bash_2"]
    go.touch()


async def test_a_conversation_without_jobs_lists_none(served):
    res = await served.client.get(served.base)
    assert res.status_code == 200
    assert res.json()["jobs"] == [] and res.json()["running"] == 0


# --------------------------------------------------------------------------
# tail
# --------------------------------------------------------------------------


async def test_a_tail_picks_up_where_the_last_one_ended(served, tmp_path):
    s = served
    go, wait_for_go = gate_file(tmp_path)
    await start(s.ctx, f"echo first; {wait_for_go}; echo second")
    job = table(s).get("bash_1")
    assert await await_until(lambda: job.written() > 0)

    one = await tail(s, "bash_1")
    assert one["text"] == "first\n" and one["status"] == RUNNING
    assert one["start"] == 0 and one["next"] == one["end"] == len(b"first\n") and one["gap"] == 0

    again = await tail(s, "bash_1", since=one["next"])
    assert again["text"] == "" and again["next"] == one["next"]

    go.touch()
    assert await await_until(lambda: not job.running)
    two = await tail(s, "bash_1", since=one["next"])
    assert two["text"] == "second\n" and two["status"] == EXITED and two["exit_code"] == 0


async def test_reading_the_tail_leaves_the_model_s_unread_output_unread(served, tmp_path):
    s = served
    await start(s.ctx, "echo for the model; exit 1")
    job = table(s).get("bash_1")
    assert await await_until(lambda: not job.running)
    unread = job.unread_bytes()

    watched = await tail(s, "bash_1")
    await s.client.get(s.base)

    assert "for the model" in watched["text"]
    assert job.unread_bytes() == unread > 0
    # The ending is still news to the model: the panel saw it, the model did not.
    notices = table(s).exit_notices()
    assert len(notices) == 1 and f"{unread} bytes" in notices[0]
    assert "for the model" in await read(s.ctx, "bash_1")


async def test_a_bounded_tail_is_the_newest_bytes_and_counts_the_rest(served):
    s = served
    await start(s.ctx, "for i in $(seq 1 400); do echo line $i; done")
    job = table(s).get("bash_1")
    assert await await_until(lambda: not job.running)

    res = await tail(s, "bash_1", limit=64)

    assert res["text"].endswith("line 400\n") and len(res["text"]) <= 64
    assert res["gap"] == res["start"] == job.written() - len(res["text"].encode())
    assert res["next"] == res["end"] == job.written()


async def test_a_tail_after_the_ring_let_go_says_how_much(served):
    s = served
    small_table(s, buffer_bytes=101)
    # Six bytes a line, 'é' two of them: the ring's edge lands inside one.
    await start(s.ctx, "for i in $(seq 1 100); do printf 'é%03d\\n' $i; done")
    job = table(s).get("bash_1")
    assert await await_until(lambda: not job.running)

    res = await tail(s, "bash_1")

    assert res["dropped"] == job.written() - 101 > 0
    assert res["gap"] >= res["dropped"]
    assert "�" not in res["text"] and res["text"].endswith("é100\n")
    assert res["start"] == res["gap"]


async def test_a_tail_keeps_escape_codes_and_holds_back_half_a_character(served, tmp_path):
    s = served
    go, wait_for_go = gate_file(tmp_path)
    await start(s.ctx, f"printf '\\033[31mred\\033[0m \\303'; {wait_for_go}; printf '\\251\\n'")
    job = table(s).get("bash_1")
    assert await await_until(lambda: job.written() == len(b"\x1b[31mred\x1b[0m \xc3"))

    first = await tail(s, "bash_1")
    # The renderer is a terminal: colour and redraws are its to apply.
    assert first["text"] == "\x1b[31mred\x1b[0m "
    assert first["next"] == first["end"] - 1

    go.touch()
    assert await await_until(lambda: not job.running)
    rest = await tail(s, "bash_1", since=first["next"])
    assert rest["text"] == "é\n"


# --------------------------------------------------------------------------
# kill
# --------------------------------------------------------------------------


async def test_a_kill_from_the_panel_ends_the_tree_and_says_who(served, tmp_path):
    s = served
    marker = tmp_path / "up"
    await start(s.ctx, f'echo up > "{marker.as_posix()}"; sleep 60')
    job = table(s).get("bash_1")
    assert await await_until(marker.exists)

    res = await s.client.post(f"{s.base}/bash_1/kill")

    assert res.status_code == 200
    body = res.json()
    assert body["killed"] is True
    assert body["job"]["status"] == KILLED and body["job"]["killed_by"] == "user"
    assert job.status == KILLED and not table(s).running()
    assert await await_until(lambda: bool(logged(s, "bash_job_done")))
    done = logged(s, "bash_job_done")
    assert len(done) == 1 and done[0]["status"] == KILLED and done[0]["killed_by"] == "user"
    assert any("killed from the Jobs tab" in e["text"] for e in logged(s, "system_note"))
    # The model hears it at its next turn, in words that say it was not its doing.
    notices = table(s).exit_notices()
    assert len(notices) == 1 and "killed by the user" in notices[0]


async def test_killing_a_job_that_already_ended_changes_nothing(served):
    s = served
    await start(s.ctx, "exit 0")
    job = table(s).get("bash_1")
    assert await await_until(lambda: not job.running)
    assert await await_until(lambda: bool(logged(s, "bash_job_done")))

    res = await s.client.post(f"{s.base}/bash_1/kill")

    assert res.status_code == 200
    assert res.json()["killed"] is False
    assert job.status == EXITED and job.killed_by == ""
    await asyncio.sleep(0.1)
    assert len(logged(s, "bash_job_done")) == 1
    assert "killed_by" not in logged(s, "bash_job_done")[0]


async def test_the_model_s_own_kill_is_not_put_down_to_the_user(served, tmp_path):
    s = served
    go, wait_for_go = gate_file(tmp_path)
    await start(s.ctx, wait_for_go)
    job = table(s).get("bash_1")
    await asyncio.to_thread(job.kill)
    assert await await_until(lambda: bool(logged(s, "bash_job_done")))
    assert "killed_by" not in logged(s, "bash_job_done")[0]
    assert not any("Jobs tab" in e["text"] for e in logged(s, "system_note"))
    assert "by the user" not in job.outcome()


# --------------------------------------------------------------------------
# what is refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["bash_0", "bash_1x", "bash_", "bash-1", "BASH_1", "bash_1234567890"])
async def test_a_job_id_that_is_not_one_is_refused(served, job_id):
    s = served
    assert (await s.client.get(f"{s.base}/{job_id}/output")).status_code == 400
    assert (await s.client.post(f"{s.base}/{job_id}/kill")).status_code == 400


async def test_unknown_ids_are_not_found_and_open_nothing(served):
    s = served
    assert (await s.client.get(f"{s.base}/bash_9/output")).status_code == 404
    assert (await s.client.post(f"{s.base}/bash_9/kill")).status_code == 404
    assert (await s.client.get("/api/conversations/not-open/jobs")).status_code == 404
    assert s.manager.get("not-open") is None
    assert (await s.client.get("/api/conversations/a.b/jobs")).status_code == 400


@pytest.mark.parametrize("params", [{"since": -1}, {"limit": 0}, {"limit": 256 * 1024 + 1}])
async def test_a_tail_out_of_bounds_is_refused(served, params):
    s = served
    await start(s.ctx, "echo hi")
    res = await s.client.get(f"{s.base}/bash_1/output", params=params)
    assert res.status_code == 400


async def test_a_job_that_was_let_go_says_so(served):
    s = served
    jobs = small_table(s, max_retained=1)
    for n in range(3):
        await start(s.ctx, "exit 0")
        job = jobs.get(f"bash_{n + 1}")
        assert await await_until(lambda job=job: not job.running)

    assert (await s.client.get(f"{s.base}/bash_1/output")).status_code == 410
    assert [r["id"] for r in (await s.client.get(s.base)).json()["jobs"]] == ["bash_2", "bash_3"]


# --------------------------------------------------------------------------
# live notes
# --------------------------------------------------------------------------


async def test_new_output_reaches_a_watching_window_throttled_and_unlogged(served):
    s = served
    window = Client()
    s.conv.clients.add(window)
    try:
        await start(s.ctx, "for i in $(seq 1 30); do echo $i; sleep 0.03; done")
        job = table(s).get("bash_1")
        assert await await_until(lambda: not job.running)
        await asyncio.sleep(0.4)  # the trailing note
    finally:
        s.conv.clients.discard(window)

    events = []
    while not window.queue.empty():
        events.append(json.loads(window.queue.get_nowait()))
    notes = [e for e in events if e["type"] == "bash_job_output"]
    # ~0.9 s of output at one note per quarter second: a handful, not thirty.
    assert 1 <= len(notes) <= 8
    assert all(e["job_id"] == "bash_1" and "seq" not in e for e in notes)
    assert notes[-1]["bytes"] == job.written()
    assert not logged(s, "bash_job_output")
