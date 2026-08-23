"""Claim-gate regression: null-owner ready cards (board card cbca4c17).

A kanban move to the ready column folds to CLAIMED with no owner; such a
card must be claimable, while doing/in_progress cards keep refusing a
different claimant.
"""

from __future__ import annotations

import pytest

from skcoord.coordination import Board, Task, TaskStatus
from skcoord.lifecycle import transition_task


@pytest.fixture(autouse=True)
def _store_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the event-sourced card store, the read path the bug lives on."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")


def _task(board: Board, task_id: str, **kw) -> Task:
    task = Task(id=task_id, title=f"Task {task_id}", created_by="tester", **kw)
    board.create_task(task)
    return task


def _view(board: Board, task_id: str):
    return next(v for v in board.get_task_views() if v.task.id == task_id)


# --- cbca4c17: null-owner claim after kanban move ---------------------------


def test_move_ready_then_claim_succeeds(tmp_path) -> None:
    """A kanban move to ready derives CLAIMED with no owner; it must be claimable."""
    board = Board(tmp_path)
    task = _task(board, "ready001")
    transition_task(tmp_path, task_id=task.id, column="ready", actor="kanban-ops")

    view = _view(board, task.id)
    assert view.status == TaskStatus.CLAIMED
    assert view.claimed_by is None

    agent = board.claim_task("jarvis", task.id)
    assert task.id in agent.claimed_tasks
    assert agent.current_task == task.id
    assert _view(board, task.id).claimed_by == "jarvis"


def test_move_doing_ownerless_is_claimable(tmp_path) -> None:
    """47e8d509 reversed this: an ownerless doing card is unclaimed, so it is
    claimable (while still SHOWING as doing). Owned doing coverage lives in
    test_owned_claim_still_blocks_other_agent below and in
    test_claim_doing_review_null_owner.py."""
    board = Board(tmp_path)
    task = _task(board, "doing001")
    transition_task(tmp_path, task_id=task.id, column="doing", actor="opus")

    assert _view(board, task.id).status == TaskStatus.IN_PROGRESS
    assert _view(board, task.id).claimed_by is None
    board.claim_task("jarvis", task.id)
    assert _view(board, task.id).claimed_by == "jarvis"


def test_owned_claim_still_blocks_other_agent(tmp_path) -> None:
    """The null-owner relaxation must not weaken the owned-claim gate."""
    board = Board(tmp_path)
    task = _task(board, "owned001")
    board.claim_task("opus", task.id)
    with pytest.raises(ValueError, match="already in_progress by opus"):
        board.claim_task("jarvis", task.id)
