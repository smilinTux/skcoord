from __future__ import annotations

from pathlib import Path

import pytest

from skcoord.card import Column
from skcoord.card_store import CardStore
from skcoord.coordination import Board, Task
from skcoord.lifecycle import transition_task

REQUIRED = (
    "ci_check_docs",
    "ci_check_gitleaks",
    "ci_check_lint",
    "ci_check_shim_imports",
    "ci_check_python311",
    "ci_check_python312",
)


def _card(home: Path, title: str) -> CardStore:
    board = Board(home)
    board.create_task(Task(id="parent01", title="source"))
    board.create_task(
        Task(
            id="review01",
            title=title,
            description="fixture",
            tags=["parent-parent01"],
        )
    )
    return CardStore(home)


def _link(store: CardStore, key: str, value: str) -> None:
    store.append_event(
        "review01", "link", "reviewer", link_key=key, link_value=value
    )


def _pass(store: CardStore) -> None:
    _link(store, "verdict", "PASS")
    for key in REQUIRED:
        _link(store, key, "SUCCESS")


@pytest.mark.parametrize("title", ["[X][REVIEW] x", "[X][REREVIEW] x"])
@pytest.mark.parametrize(
    "route", ["complete", "move", "move_enum", "lifecycle", "board"]
)
def test_every_direct_terminal_route_accepts_only_exact_pass(
    tmp_path: Path, title: str, route: str
) -> None:
    store = _card(tmp_path, title)
    _pass(store)
    if route == "complete":
        store.append_event("review01", "complete", "reviewer")
    elif route in {"move", "move_enum"}:
        column = Column.DONE if route == "move_enum" else "done"
        store.append_event("review01", "move", "reviewer", column=column)
    elif route == "lifecycle":
        transition_task(tmp_path, task_id="review01", column="done", actor="reviewer")
    else:
        Board(tmp_path).complete_task("reviewer", "review01")
    assert store.fold("review01").status.value == "done"


@pytest.mark.parametrize("title", ["[X][REVIEW] x", "[X][REREVIEW] x"])
@pytest.mark.parametrize(
    "route", ["complete", "move", "move_enum", "lifecycle", "board"]
)
@pytest.mark.parametrize("verdict", [None, "pass", "PASS ", "PASSED", "FAIL", "BLOCKED"])
def test_every_terminal_route_rejects_noncanonical_verdict(
    tmp_path: Path, title: str, route: str, verdict: str | None
) -> None:
    store = _card(tmp_path, title)
    if verdict is not None:
        _link(store, "verdict", verdict)
    for key in REQUIRED:
        _link(store, key, "SUCCESS")
    with pytest.raises(ValueError, match="canonical verdict PASS"):
        if route == "complete":
            store.append_event("review01", "complete", "reviewer")
        elif route in {"move", "move_enum"}:
            column = Column.DONE if route == "move_enum" else "done"
            store.append_event("review01", "move", "reviewer", column=column)
        else:
            if route == "lifecycle":
                transition_task(
                    tmp_path,
                    task_id="review01",
                    column="done",
                    actor="reviewer",
                )
            else:
                Board(tmp_path).complete_task("reviewer", "review01")
    assert store.fold("review01").status.value != "done"


@pytest.mark.parametrize("missing", REQUIRED)
@pytest.mark.parametrize("state", [None, "FAILURE", "PENDING", "success", " SUCCESS "])
def test_required_ci_is_present_and_exact_success(
    tmp_path: Path, missing: str, state: str | None
) -> None:
    store = _card(tmp_path, "[X][REVIEW] x")
    _link(store, "verdict", "PASS")
    for key in REQUIRED:
        if key != missing:
            _link(store, key, "SUCCESS")
    if state is not None:
        _link(store, missing, state)
    with pytest.raises(ValueError, match="not exact SUCCESS"):
        store.append_event("review01", "complete", "reviewer")


@pytest.mark.parametrize(
    "route", ["complete", "move", "move_enum", "lifecycle", "board"]
)
def test_ordinary_cards_keep_existing_terminal_behavior(tmp_path: Path, route: str) -> None:
    store = _card(tmp_path, "ordinary work")
    if route == "complete":
        store.append_event("review01", "complete", "worker")
    elif route in {"move", "move_enum"}:
        column = Column.DONE if route == "move_enum" else "done"
        store.append_event("review01", "move", "worker", column=column)
    elif route == "lifecycle":
        transition_task(tmp_path, task_id="review01", column="done", actor="worker")
    else:
        board = Board(tmp_path)
        board.create_task(Task(id="legacy01", title="ordinary legacy work"))
        board.claim_task("worker", "legacy01")
        board.complete_task("worker", "legacy01")
        assert CardStore(tmp_path).fold("legacy01").status.value == "done"
        return
    assert store.fold("review01").status.value == "done"
