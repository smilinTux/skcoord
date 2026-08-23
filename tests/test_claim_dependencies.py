"""Dependency enforcement at claim time (board card 34be7725).

Board.claim_task refuses to claim a task whose dependencies are not done,
listing every incomplete ID. The compatibility force=True flag cannot override
dependency gates. Unknown dependency IDs fail closed because they can never be
verified done.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from skcoord.coordination import Board, Task
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


# --- 34be7725: dependency enforcement at claim time -------------------------


def test_claim_with_done_dependency_succeeds(tmp_path) -> None:
    board = Board(tmp_path)
    dep = _task(board, "dep00001")
    board.claim_task("opus", dep.id)
    board.complete_task("opus", dep.id)
    task = _task(board, "main0001", dependencies=[dep.id])

    agent = board.claim_task("jarvis", task.id)
    assert task.id in agent.claimed_tasks


def test_claim_with_incomplete_dependencies_lists_ids(tmp_path) -> None:
    """open/claimed/in_progress/review dependencies all block, with IDs listed."""
    board = Board(tmp_path)
    open_dep = _task(board, "depopen1")
    ready_dep = _task(board, "depready")
    transition_task(tmp_path, task_id=ready_dep.id, column="ready", actor="ops")
    doing_dep = _task(board, "depdoing")
    board.claim_task("opus", doing_dep.id)
    review_dep = _task(board, "depreview")
    board.claim_task("opus", review_dep.id)
    transition_task(tmp_path, task_id=review_dep.id, column="review", actor="ops")

    task = _task(
        board,
        "main0002",
        dependencies=[open_dep.id, ready_dep.id, doing_dep.id, review_dep.id],
    )
    with pytest.raises(ValueError) as excinfo:
        board.claim_task("jarvis", task.id)
    message = str(excinfo.value)
    for dep_id in (open_dep.id, ready_dep.id, doing_dep.id, review_dep.id):
        assert dep_id in message
    agent = board.load_agent("jarvis")
    assert agent is None or task.id not in agent.claimed_tasks


@pytest.mark.parametrize("store_mode", ["0", "1", "dual"])
def test_force_claim_cannot_bypass_incomplete_unknown_review_or_human_dependencies(
    tmp_path, monkeypatch: pytest.MonkeyPatch, store_mode: str
) -> None:
    """Force preserves every dependency gate across supported store modes."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", store_mode)
    board = Board(tmp_path)
    incomplete = _task(board, "depopen2")
    review = _task(board, "deprevw2")
    board.claim_task("opus", review.id)
    transition_task(tmp_path, task_id=review.id, column="review", actor="ops")
    human = _task(board, "dephuman", tags=["human-gate"])
    task = _task(
        board,
        "main0003",
        dependencies=[incomplete.id, review.id, human.id, "nosuch00"],
    )

    with pytest.raises(ValueError) as excinfo:
        board.claim_task("jarvis", task.id, force=True)
    message = str(excinfo.value)
    for dependency in (*task.dependencies,):
        assert dependency in message
    agent = board.load_agent("jarvis")
    assert agent is None or task.id not in agent.claimed_tasks


def test_force_claim_with_done_dependency_still_succeeds(tmp_path) -> None:
    """The compatibility flag remains accepted after every gate is done."""
    board = Board(tmp_path)
    dependency = _task(board, "depdone2")
    board.claim_task("opus", dependency.id)
    board.complete_task("opus", dependency.id)
    task = _task(board, "main0004", dependencies=[dependency.id])

    agent = board.claim_task("jarvis", task.id, force=True)
    assert task.id in agent.claimed_tasks


def test_archived_done_dependency_does_not_block(tmp_path) -> None:
    """A dependency completed and then archived still counts as done."""
    board = Board(tmp_path)
    dep = _task(board, "dep00003")
    board.claim_task("opus", dep.id)
    board.complete_task("opus", dep.id)
    board.archive_task(dep.id, by="tester")
    task = _task(board, "main0005", dependencies=[dep.id])

    agent = board.claim_task("jarvis", task.id)
    assert task.id in agent.claimed_tasks


def test_completion_enforces_append_only_dependency_added_after_claim(tmp_path) -> None:
    """A gate appended after claim blocks normal completion until it is done."""
    board = Board(tmp_path)
    gate = _task(board, "gatecomp1")
    target = _task(board, "complete1")
    board.claim_task("implementer", target.id)

    from skcoord.card_store import add_dependency

    assert add_dependency(
        tmp_path,
        target.id,
        gate.id,
        agent="reviewer",
        reason="completion must honor the appended review gate",
    )
    with pytest.raises(ValueError, match=gate.id):
        board.complete_task("implementer", target.id)

    board.claim_task("reviewer", gate.id)
    board.complete_task("reviewer", gate.id)
    completed = board.complete_task("implementer", target.id)
    assert target.id in completed.completed_tasks


def test_completion_enforces_appended_dependency_in_legacy_rollback_mode(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback reads retain append-only completion gates from the CardStore."""
    board = Board(tmp_path)
    gate = _task(board, "gateroll1")
    target = _task(board, "comproll1")
    board.claim_task("implementer", target.id)

    from skcoord.card_store import add_dependency

    assert add_dependency(
        tmp_path,
        target.id,
        gate.id,
        agent="reviewer",
        reason="legacy rollback must preserve the completion gate",
    )
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    with pytest.raises(ValueError, match=gate.id):
        board.complete_task("implementer", target.id)


@pytest.mark.parametrize(
    ("target_state", "expected_status"),
    [("claimed", "claimed"), ("doing", "in_progress"), ("review", "review")],
)
def test_completion_blocks_incomplete_dependencies_in_active_states(
    tmp_path, target_state: str, expected_status: str
) -> None:
    """Claimed, doing, and review cards cannot complete around an open gate."""
    board = Board(tmp_path)
    gate = _task(board, "gatecomp2")
    target = _task(board, "complete2")
    board.claim_task("implementer", target.id)
    from skcoord.card_store import add_dependency

    assert add_dependency(
        tmp_path,
        target.id,
        gate.id,
        agent="reviewer",
        reason="completion must honor an appended gate in every active state",
    )
    if target_state == "claimed":
        parked = _task(board, "parked001")
        board.claim_task("implementer", parked.id)
    elif target_state == "review":
        transition_task(tmp_path, task_id=target.id, column="review", actor="reviewer")

    view = next(view for view in board.get_task_views() if view.task.id == target.id)
    assert view.status.value == expected_status
    with pytest.raises(ValueError, match=gate.id):
        board.complete_task("implementer", target.id)


def test_completion_lists_unknown_open_and_review_dependencies(tmp_path) -> None:
    """Completion reports every unknown or not-done folded dependency."""
    board = Board(tmp_path)
    open_gate = _task(board, "gateopen1")
    review_gate = _task(board, "gaterev01")
    board.claim_task("reviewer", review_gate.id)
    transition_task(tmp_path, task_id=review_gate.id, column="review", actor="reviewer")
    target = _task(board, "complete3")
    board.claim_task("implementer", target.id)
    from skcoord.card_store import CardStore, add_dependency

    for dependency_id in (open_gate.id, review_gate.id):
        assert add_dependency(
            tmp_path,
            target.id,
            dependency_id,
            agent="reviewer",
            reason="completion must list every appended incomplete dependency",
        )
    # The supported amendment API rejects unknown IDs.  A stale external event
    # is still fail-closed at completion time.
    CardStore(tmp_path).append_event(
        target.id,
        "add_dependency",
        "recovery",
        dependency="unknown1",
        reason="simulate a pre-validation historical event",
    )

    with pytest.raises(ValueError) as excinfo:
        board.complete_task("implementer", target.id)
    message = str(excinfo.value)
    for dependency_id in (open_gate.id, review_gate.id, "unknown1"):
        assert dependency_id in message


def test_completion_accepts_archived_done_dependency_and_recompletion_is_idempotent(
    tmp_path,
) -> None:
    """Archived done gates satisfy completion and a done task remains a no-op."""
    board = Board(tmp_path)
    archived_gate = _task(board, "gatearch1")
    board.claim_task("reviewer", archived_gate.id)
    board.complete_task("reviewer", archived_gate.id)
    board.archive_task(archived_gate.id, by="reviewer")
    target = _task(board, "complete4", dependencies=[archived_gate.id])
    board.claim_task("implementer", target.id)
    first = board.complete_task("implementer", target.id)

    from skcoord.card_store import add_dependency

    later_gate = _task(board, "gatelater")
    assert add_dependency(
        tmp_path,
        target.id,
        later_gate.id,
        agent="reviewer",
        reason="recompletion must retain existing done idempotency",
    )
    repeated = board.complete_task("implementer", target.id)
    assert first.agent == repeated.agent
    assert repeated.completed_tasks.count(target.id) == 1


def test_release_claim_is_targeted_idempotent_and_does_not_complete(tmp_path) -> None:
    board = Board(tmp_path)
    first = _task(board, "release01")
    second = _task(board, "release02")
    board.claim_task("probe", first.id)
    board.claim_task("probe", second.id)

    assert board.release_claim("probe", first.id, actor="repair")
    owner = board.load_agent("probe")
    assert owner is not None
    assert first.id not in owner.claimed_tasks
    assert second.id in owner.claimed_tasks
    assert first.id not in owner.completed_tasks
    assert not board.release_claim("probe", first.id, actor="repair")
    view = next(view for view in board.get_task_views() if view.task.id == first.id)
    assert view.status.value == "open"
