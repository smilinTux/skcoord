"""Acceptance criteria are immutable birth facts with folded amendments."""

from __future__ import annotations

from skcoord.card import KanbanBoard
from skcoord.card_store import CardCore, CardStore, export_to_legacy
from skcoord.coordination import Board, Task


def _core_bytes(store: CardStore, card_id: str) -> bytes:
    """Read the immutable core file exactly as stored."""
    return (store.cards_dir / card_id / "core.json").read_bytes()


def test_fold_seeds_acceptance_criteria_from_immutable_core(tmp_path):
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="criteria-core",
            title="Card",
            acceptance_criteria=["birth one", "birth two"],
        )
    )
    before = _core_bytes(store, "criteria-core")

    card = store.fold("criteria-core")

    assert card is not None
    assert card.acceptance_criteria == ["birth one", "birth two"]
    assert _core_bytes(store, "criteria-core") == before


def test_fold_applies_latest_criteria_amendment_without_rewriting_core(tmp_path):
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="criteria-amend",
            title="Card",
            acceptance_criteria=["birth criterion"],
        )
    )
    before = _core_bytes(store, "criteria-amend")
    store.append_event(
        "criteria-amend", "amend_criteria", "reviewer", criteria=["first amendment"]
    )
    store.append_event(
        "criteria-amend",
        "amend_criteria",
        "reviewer",
        criteria=["current one", "current two"],
    )

    card = store.fold("criteria-amend")

    assert card is not None
    assert card.acceptance_criteria == ["current one", "current two"]
    assert _core_bytes(store, "criteria-amend") == before


def test_board_task_view_exposes_folded_acceptance_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(
        Task(
            id="criteria-view",
            title="Card",
            acceptance_criteria=["birth criterion"],
        )
    )
    store = CardStore(tmp_path)
    before = _core_bytes(store, "criteria-view")
    store.append_event(
        "criteria-view",
        "amend_criteria",
        "reviewer",
        criteria=["folded criterion"],
    )

    view = next(view for view in board.get_task_views() if view.task.id == "criteria-view")

    assert view.task.acceptance_criteria == ["folded criterion"]
    assert _core_bytes(store, "criteria-view") == before


def test_kill_switch_card_projection_preserves_birth_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(
        Task(
            id="criteria-kill-switch",
            title="Card",
            acceptance_criteria=["birth one", "birth two"],
        )
    )
    store_card = next(
        card for card in KanbanBoard(tmp_path).cards() if card.id == "criteria-kill-switch"
    )

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    legacy_card = next(
        card for card in KanbanBoard(tmp_path).cards() if card.id == "criteria-kill-switch"
    )

    assert legacy_card.acceptance_criteria == store_card.acceptance_criteria
    assert legacy_card.acceptance_criteria == ["birth one", "birth two"]


def test_rollback_export_preserves_current_folded_criteria(tmp_path):
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="criteria-export",
            title="Store-born card",
            acceptance_criteria=["birth criterion"],
        )
    )
    before = _core_bytes(store, "criteria-export")
    store.append_event(
        "criteria-export",
        "amend_criteria",
        "reviewer",
        criteria=["current criterion"],
    )

    result = export_to_legacy(tmp_path)
    exported = next(task for task in Board(tmp_path).load_tasks() if task.id == "criteria-export")

    assert result["tasks_written"] == 1
    assert exported.acceptance_criteria == ["current criterion"]
    assert _core_bytes(store, "criteria-export") == before
