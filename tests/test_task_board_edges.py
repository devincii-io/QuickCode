"""Task board updates that used to leave it in a state nothing could leave.

A board is coordination state: a teammate that finds every remaining task
blocked has nothing to do, forever, and there is no ``remove_blocked_by`` to
undo an edge. So the edges that make a task permanently unstartable -- a
cycle, a blocker that was deleted -- and the half-applied updates that
silently add edges nobody asked for are the bugs worth pinning.
"""

from __future__ import annotations

import pytest

from quickcode.core.tasks import TaskBoard


def board_of(n: int) -> TaskBoard:
    board = TaskBoard()
    for i in range(n):
        board.create(f"task {i + 1}")
    return board


def test_a_dependency_cycle_is_refused():
    board = board_of(3)
    board.update("T2", add_blocked_by=["T1"])
    board.update("T3", add_blocked_by=["T2"])

    with pytest.raises(ValueError, match="cycle"):
        board.update("T1", add_blocked_by=["T3"])
    with pytest.raises(ValueError, match="cycle"):
        board.update("T3", add_blocks=["T1"])

    assert board.get("T1").blocked_by == []
    assert board.get("T3").blocks == []


def test_a_mutual_block_is_refused_either_way_round():
    board = board_of(2)
    board.update("T1", add_blocks=["T2"])

    with pytest.raises(ValueError, match="cycle"):
        board.update("T1", add_blocked_by=["T2"])


def test_an_unknown_id_in_a_list_changes_nothing():
    """The known ids before it used to be linked anyway, then the error was
    reported as if the whole update had failed."""
    board = board_of(3)

    with pytest.raises(KeyError):
        board.update("T3", add_blocked_by=["T1", "T9", "T2"])

    assert board.get("T3").blocked_by == []
    assert board.get("T1").blocks == []


def test_a_refused_status_change_does_not_keep_the_edges_that_came_with_it():
    board = board_of(2)

    with pytest.raises(ValueError):
        board.update("T2", add_blocked_by=["T1"], status="in_progress", owner="ann")

    assert board.get("T2").blocked_by == []
    assert board.get("T2").owner is None
    assert board.get("T1").blocks == []


def test_a_deleted_blocker_no_longer_blocks():
    """Deleting the task that blocks another is how work gets dropped; the
    dependent used to stay unstartable and unclaimable for good."""
    board = board_of(2)
    board.update("T2", add_blocked_by=["T1"])
    board.update("T1", status="deleted")

    assert [t.id for t in board.claimable()] == ["T2"]
    assert board.update("T2", status="in_progress").status == "in_progress"
    assert "T1" not in board.render_table()


def test_an_id_is_found_whatever_its_case_or_padding():
    board = board_of(1)

    assert board.get(" t1 ").id == "T1"
    assert board.update("t1", status="completed").status == "completed"
