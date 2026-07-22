import asyncio
from pathlib import Path

import pytest

from quickcode.core.tasks import TaskBoard
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.task import (
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
)


def make_ctx(tmp_path: Path) -> ToolCtx:
    return ToolCtx(cwd=tmp_path, read_registry=ReadRegistry())


# --- TaskBoard core -----------------------------------------------------


def test_create_assigns_sequential_ids():
    board = TaskBoard()
    t1 = board.create("first")
    t2 = board.create("second")
    assert t1.id == "T1"
    assert t2.id == "T2"
    ids = [t.id for t in board.list()]
    assert ids == ["T1", "T2"]


def test_dependency_gate_and_reciprocal_edge():
    board = TaskBoard()
    t1 = board.create("blocker")
    t2 = board.create("blocked")

    board.update(t2.id, add_blocked_by=[t1.id])

    with pytest.raises(ValueError):
        board.update(t2.id, status="in_progress")

    board.update(t1.id, status="in_progress")
    board.update(t1.id, status="completed")

    updated = board.update(t2.id, status="in_progress")
    assert updated.status == "in_progress"

    # reciprocal edge
    assert t2.id in board.get(t1.id).blocks


def test_update_unknown_id_raises_keyerror():
    board = TaskBoard()
    with pytest.raises(KeyError):
        board.update("T99", status="completed")


def test_save_load_round_trip_preserves_tasks_and_counter(tmp_path: Path):
    path = tmp_path / "board.json"
    board = TaskBoard(path=path)
    board.create("first")
    board.create("second")

    loaded = TaskBoard.load(path)
    assert [t.id for t in loaded.list()] == ["T1", "T2"]
    assert loaded.list()[0].subject == "first"

    t3 = loaded.create("third")
    assert t3.id == "T3"


def test_load_missing_file_returns_empty_board(tmp_path: Path):
    path = tmp_path / "does_not_exist" / "board.json"
    board = TaskBoard.load(path)
    assert board.list() == []
    t1 = board.create("first")
    assert t1.id == "T1"


def test_claimable():
    board = TaskBoard()
    t1 = board.create("blocker")
    t2 = board.create("blocked")
    board.update(t2.id, add_blocked_by=[t1.id])

    claimable_ids = [t.id for t in board.claimable()]
    assert t1.id in claimable_ids
    assert t2.id not in claimable_ids

    board.update(t1.id, status="in_progress")
    board.update(t1.id, status="completed")

    claimable_ids = [t.id for t in board.claimable()]
    assert t2.id in claimable_ids


def test_render_checklist_formatting():
    board = TaskBoard()
    t1 = board.create("do the thing")
    t2 = board.create("blocked thing")
    t3 = board.create("in progress thing")
    board.update(t2.id, add_blocked_by=[t1.id])
    board.update(t3.id, status="in_progress")
    board.update(t1.id, status="in_progress")
    board.update(t1.id, status="completed")

    checklist = board.render_checklist()
    lines = checklist.splitlines()
    assert f"[x] {t1.id} do the thing" in lines
    assert f"[~] {t3.id} in progress thing" in lines
    blocked_line = next(line for line in lines if line.startswith(f"[ ] {t2.id}"))
    assert "blocked by" not in blocked_line or f"({t1.id}" not in blocked_line
    # t1 is completed so t2 should no longer show as blocked
    assert blocked_line == f"[ ] {t2.id} blocked thing"


def test_render_checklist_shows_incomplete_blocker():
    board = TaskBoard()
    t1 = board.create("blocker")
    t2 = board.create("blocked")
    board.update(t2.id, add_blocked_by=[t1.id])

    checklist = board.render_checklist()
    line = next(line for line in checklist.splitlines() if line.startswith(f"[ ] {t2.id}"))
    assert f"(blocked by {t1.id})" in line


def test_render_checklist_empty():
    board = TaskBoard()
    assert board.render_checklist() == "(no tasks)"


def test_delete_excluded_from_list_by_default():
    board = TaskBoard()
    t1 = board.create("to delete")
    board.update(t1.id, status="deleted")
    assert board.list() == []
    assert [t.id for t in board.list(include_deleted=True)] == [t1.id]


# --- Tools ----------------------------------------------------------------


def test_tools_share_board_across_calls_via_ctx_extra(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    create_tool = TaskCreateTool()
    list_tool = TaskListTool()

    result1 = asyncio.run(
        create_tool.run(create_tool.Input(subject="write tests"), ctx)
    )
    assert "T1" in result1.content
    assert not result1.is_error

    result2 = asyncio.run(
        create_tool.run(create_tool.Input(subject="run tests"), ctx)
    )
    assert "T2" in result2.content

    list_result = asyncio.run(list_tool.run(list_tool.Input(), ctx))
    assert "T1" in list_result.content
    assert "T2" in list_result.content
    assert "write tests" in list_result.content
    assert "run tests" in list_result.content


def test_task_update_bad_id_returns_error_not_raise(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    update_tool = TaskUpdateTool()
    result = asyncio.run(
        update_tool.run(update_tool.Input(task_id="T99", status="completed"), ctx)
    )
    assert result.is_error
    assert "T99" in result.content


def test_task_update_dependency_violation_returns_error(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    create_tool = TaskCreateTool()
    update_tool = TaskUpdateTool()

    asyncio.run(create_tool.run(create_tool.Input(subject="blocker"), ctx))
    asyncio.run(create_tool.run(create_tool.Input(subject="blocked"), ctx))

    asyncio.run(
        update_tool.run(update_tool.Input(task_id="T2", add_blocked_by=["T1"]), ctx)
    )
    result = asyncio.run(
        update_tool.run(update_tool.Input(task_id="T2", status="in_progress"), ctx)
    )
    assert result.is_error
    assert "T1" in result.content


def test_task_get_full_detail(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    create_tool = TaskCreateTool()
    get_tool = TaskGetTool()

    asyncio.run(
        create_tool.run(
            create_tool.Input(
                subject="fix bug", description="the auth bug", active_form="Fixing auth bug"
            ),
            ctx,
        )
    )
    result = asyncio.run(get_tool.run(get_tool.Input(task_id="T1"), ctx))
    assert "fix bug" in result.content
    assert "the auth bug" in result.content
    assert "Fixing auth bug" in result.content
    assert "pending" in result.content


def test_task_list_empty_board(tmp_path: Path):
    ctx = make_ctx(tmp_path)
    list_tool = TaskListTool()
    result = asyncio.run(list_tool.run(list_tool.Input(), ctx))
    assert result.content == "(no tasks)"
