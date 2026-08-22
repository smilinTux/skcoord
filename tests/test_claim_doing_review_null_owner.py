"""Claim-gate regression: null-owner doing/review cards (board card 47e8d509).

A kanban move to doing or review carries no owner, so the card folds to
IN_PROGRESS/REVIEW with claimed_by None. Like the ready-column case fixed
under cbca4c17, such a card is unclaimed and must be claimable — while an
OWNED doing/review card must keep refusing a different claimant. Extends the
pattern of test_claim_ready_null_owner.py.
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


# --- 47e8d509: null-owner claim after kanban move to doing/review -----------


def test_move_doing_then_claim_succeeds(tmp_path) -> None:
    """A kanban move to doing derives IN_PROGRESS with no owner; it must be
    claimable, and after the claim the card still SHOWS as doing (WIP is not
    masked by folding to open)."""
    board = Board(tmp_path)
    task = _task(board, "doing002")
    transition_task(tmp_path, task_id=task.id, column="doing", actor="kanban-ops")

    view = _view(board, task.id)
    assert view.status == TaskStatus.IN_PROGRESS
    assert view.claimed_by is None

    agent = board.claim_task("jarvis", task.id)
    assert task.id in agent.claimed_tasks
    assert agent.current_task == task.id
    view = _view(board, task.id)
    assert view.claimed_by == "jarvis"
    assert view.status == TaskStatus.IN_PROGRESS


def test_move_review_then_claim_succeeds(tmp_path) -> None:
    """A kanban move to review derives REVIEW with no owner; it must be
    claimable, and the card keeps showing as review afterwards."""
    board = Board(tmp_path)
    task = _task(board, "review002")
    transition_task(tmp_path, task_id=task.id, column="review", actor="kanban-ops")

    view = _view(board, task.id)
    assert view.status == TaskStatus.REVIEW
    assert view.claimed_by is None

    agent = board.claim_task("jarvis", task.id)
    assert task.id in agent.claimed_tasks
    assert _view(board, task.id).claimed_by == "jarvis"


def test_owned_doing_still_blocks_other_claimant(tmp_path) -> None:
    """An OWNED doing card is genuine WIP and must refuse a different agent."""
    board = Board(tmp_path)
    task = _task(board, "doing003")
    board.claim_task("opus", task.id)

    assert _view(board, task.id).status == TaskStatus.IN_PROGRESS
    with pytest.raises(ValueError, match="already in_progress by opus"):
        board.claim_task("jarvis", task.id)


def test_owned_review_still_blocks_other_claimant(tmp_path) -> None:
    """An OWNED review card must refuse a different agent. Regression: before
    47e8d509, REVIEW was absent from the claim gate entirely, so any agent
    could claim a card another agent had pushed to review."""
    board = Board(tmp_path)
    task = _task(board, "review003")
    board.claim_task("opus", task.id)
    transition_task(tmp_path, task_id=task.id, column="review", actor="opus")

    view = _view(board, task.id)
    assert view.status == TaskStatus.REVIEW
    assert view.claimed_by == "opus"
    with pytest.raises(ValueError, match="already review by opus"):
        board.claim_task("jarvis", task.id)


def test_review_owner_can_reclaim_own_card(tmp_path) -> None:
    """The gate must not refuse the review owner re-claiming their own card."""
    board = Board(tmp_path)
    task = _task(board, "review004")
    board.claim_task("opus", task.id)
    transition_task(tmp_path, task_id=task.id, column="review", actor="opus")

    agent = board.claim_task("opus", task.id)
    assert task.id in agent.claimed_tasks
    assert _view(board, task.id).claimed_by == "opus"


def test_done_ownerless_still_refuses_claim(tmp_path) -> None:
    """The ownerless relaxation never applies to DONE: a completed card stays
    unclaimable even though completion drops the owner."""
    board = Board(tmp_path)
    task = _task(board, "done002")
    board.claim_task("opus", task.id)
    board.complete_task("opus", task.id)

    assert _view(board, task.id).status == TaskStatus.DONE
    with pytest.raises(ValueError, match="already done"):
        board.claim_task("jarvis", task.id)
