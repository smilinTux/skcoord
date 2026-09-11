"""Claim-gate regression: null-owner doing/review cards (board card 47e8d509).

A kanban move to doing or review carries no owner, so the card folds to
IN_PROGRESS/REVIEW with claimed_by None. Like the ready-column case fixed
under cbca4c17, such a card is unclaimed and must be claimable — while an
OWNED doing/review card must keep refusing a different claimant. Extends the
pattern of test_claim_ready_null_owner.py.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from skcoord.card_store import CardStore
from skcoord.coordination import Board, Task, TaskStatus
from skcoord.lifecycle import transition_task


@pytest.fixture(autouse=True)
def _store_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the card store and isolate lazy SKCapstone imports per test."""
    loaded_modules = set(sys.modules)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    yield
    for module_name in set(sys.modules) - loaded_modules:
        if module_name == "skcapstone" or module_name.startswith("skcapstone."):
            sys.modules.pop(module_name, None)


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


def test_active_owner_reclaim_preserves_exact_claim_generation(tmp_path) -> None:
    """A worker verifying its existing claim must not invalidate its wrapper."""
    board = Board(tmp_path)
    task = _task(board, "doing004")
    board.claim_task("opus", task.id)
    store = CardStore(tmp_path)
    before = store.fold(task.id)
    before_events = store._read_events(task.id)
    projection = board.agent_projection_path("opus")
    before_projection = projection.read_bytes()

    agent = board.claim_task("opus", task.id)

    after = store.fold(task.id)
    assert agent.current_task == task.id
    assert before is not None and after is not None
    assert after.meta["_claim_revision"] == before.meta["_claim_revision"]
    assert store._read_events(task.id) == before_events
    assert projection.read_bytes() == before_projection


def test_demoted_owner_reclaim_mints_new_generation(tmp_path) -> None:
    """Resuming a READY claim is a new active generation, not an idempotent retry."""
    board = Board(tmp_path)
    first = _task(board, "ready004")
    second = _task(board, "doing005")
    board.claim_task("opus", first.id)
    old_revision = CardStore(tmp_path).fold(first.id).meta["_claim_revision"]
    board.claim_task("opus", second.id)

    agent = board.claim_task("opus", first.id)

    resumed = CardStore(tmp_path).fold(first.id)
    assert resumed is not None
    assert agent.current_task == first.id
    assert resumed.status.value == "doing"
    assert resumed.meta["_claim_revision"] != old_revision


def test_active_owner_reclaim_repairs_missing_projection_without_new_generation(
    tmp_path,
) -> None:
    board = Board(tmp_path)
    task = _task(board, "doing006")
    board.claim_task("opus", task.id)
    store = CardStore(tmp_path)
    before = store.fold(task.id)
    before_events = store._read_events(task.id)
    board.agent_projection_path("opus").unlink()

    agent = board.claim_task("opus", task.id)

    after = store.fold(task.id)
    assert before is not None and after is not None
    assert agent.current_task == task.id
    assert after.meta["_claim_revision"] == before.meta["_claim_revision"]
    assert store._read_events(task.id) == before_events


def test_active_owner_reclaim_repairs_stale_projection_and_demotes_displaced_card(
    tmp_path,
) -> None:
    board = Board(tmp_path)
    active = _task(board, "doing007")
    displaced = _task(board, "doing008")
    board.claim_task("opus", active.id)
    store = CardStore(tmp_path)
    before = store.fold(active.id)
    before_claims = [
        event
        for event in store._read_events(active.id)
        if event.get("action") == "claim"
    ]
    agent = board.load_agent("opus")
    assert agent is not None
    agent.current_task = displaced.id
    agent.claimed_tasks.append(displaced.id)
    board.save_agent(agent)

    repaired = board.claim_task("opus", active.id)

    after = store.fold(active.id)
    displaced_after = store.fold(displaced.id)
    after_claims = [
        event
        for event in store._read_events(active.id)
        if event.get("action") == "claim"
    ]
    assert before is not None and after is not None and displaced_after is not None
    assert repaired.current_task == active.id
    assert displaced.id not in repaired.claimed_tasks
    assert after.meta["_claim_revision"] == before.meta["_claim_revision"]
    assert after_claims == before_claims
    assert displaced_after.owner is None
    assert displaced_after.status.value == "backlog"


def test_active_owner_reclaim_retains_authoritative_ready_displaced_claim(tmp_path) -> None:
    board = Board(tmp_path)
    active = _task(board, "doing011")
    displaced = _task(board, "ready011")
    board.claim_task("opus", active.id)
    store = CardStore(tmp_path)
    active_before = store.fold(active.id)
    store.append_event(
        displaced.id,
        "claim",
        "opus",
        owner="opus",
        claim_revision="ready-revision",
    )
    store.append_event(displaced.id, "move", "opus", column="ready")
    displaced_events = store._read_events(displaced.id)
    agent = board.load_agent("opus")
    assert agent is not None
    agent.current_task = displaced.id
    agent.claimed_tasks.append(displaced.id)
    board.save_agent(agent)

    repaired = board.claim_task("opus", active.id)

    active_after = store.fold(active.id)
    displaced_after = store.fold(displaced.id)
    assert active_before is not None and active_after is not None
    assert displaced_after is not None
    assert repaired.current_task == active.id
    assert displaced.id in repaired.claimed_tasks
    assert active_after.meta["_claim_revision"] == active_before.meta["_claim_revision"]
    assert displaced_after.owner == "opus"
    assert displaced_after.status.value == "ready"
    assert store._read_events(displaced.id) == displaced_events


def test_active_owner_reclaim_converges_after_durable_demote_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board(tmp_path)
    active = _task(board, "doing009")
    displaced = _task(board, "doing010")
    board.claim_task("opus", active.id)
    store = CardStore(tmp_path)
    active_before = store.fold(active.id)
    store.append_event(
        displaced.id,
        "claim",
        "opus",
        owner="opus",
        claim_revision="displaced-revision",
    )
    agent = board.load_agent("opus")
    assert agent is not None
    agent.current_task = displaced.id
    agent.claimed_tasks.append(displaced.id)
    board.save_agent(agent)
    original_append = CardStore.append_event

    def append_then_raise(self, card_id, action, actor, **payload):
        original_append(self, card_id, action, actor, **payload)
        if card_id == displaced.id and action == "move":
            raise OSError("demote bytes were written before failure")

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    repaired = board.claim_task("opus", active.id)

    active_after = store.fold(active.id)
    displaced_after = store.fold(displaced.id)
    assert active_before is not None and active_after is not None
    assert displaced_after is not None
    assert repaired.current_task == active.id
    assert active_after.meta["_claim_revision"] == active_before.meta["_claim_revision"]
    assert displaced_after.owner == "opus"
    assert displaced_after.status.value == "ready"


def test_active_owner_reclaim_fold_failure_preserves_projection_and_events(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = Board(tmp_path)
    active = _task(board, "doing011")
    displaced = _task(board, "doing012")
    board.claim_task("opus", active.id)
    store = CardStore(tmp_path)
    agent = board.load_agent("opus")
    assert agent is not None
    agent.current_task = displaced.id
    agent.claimed_tasks.append(displaced.id)
    board.save_agent(agent)
    projection = board.agent_projection_path("opus")
    before_projection = projection.read_bytes()
    before_active_events = store._read_events(active.id)
    before_displaced_events = store._read_events(displaced.id)
    original_fold = CardStore.fold

    def fail_displaced_fold(self, card_id):
        if card_id == displaced.id:
            raise OSError("fold failed")
        return original_fold(self, card_id)

    monkeypatch.setattr(CardStore, "fold", fail_displaced_fold)
    with pytest.raises(OSError, match="fold failed"):
        board.claim_task("opus", active.id)

    assert projection.read_bytes() == before_projection
    assert store._read_events(active.id) == before_active_events
    assert store._read_events(displaced.id) == before_displaced_events


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
