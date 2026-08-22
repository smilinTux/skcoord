"""Dependency enforcement at claim time (board card 34be7725).

Board.claim_task refuses to claim a task whose dependencies are not done,
listing the incomplete IDs; force=True is the explicit override. Unknown
dependency IDs fail closed (they can never be verified done).
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


def test_force_claim_overrides_dependencies(tmp_path) -> None:
    board = Board(tmp_path)
    dep = _task(board, "dep00002")
    task = _task(board, "main0003", dependencies=[dep.id])

    agent = board.claim_task("jarvis", task.id, force=True)
    assert task.id in agent.claimed_tasks


def test_unknown_dependency_blocks_by_default(tmp_path) -> None:
    """An unknown dependency ID can never be verified done, so it blocks
    (fail closed); force=True remains the documented escape hatch."""
    board = Board(tmp_path)
    task = _task(board, "main0004", dependencies=["nosuch00"])

    with pytest.raises(ValueError, match="nosuch00"):
        board.claim_task("jarvis", task.id)
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
