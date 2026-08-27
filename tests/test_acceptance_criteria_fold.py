"""Acceptance criteria are immutable birth facts with folded amendments."""

from __future__ import annotations

import pytest

from skcoord import card_store
from skcoord.card import KanbanBoard
from skcoord.card_store import CardCore, CardStore, export_to_legacy, parity_check
from skcoord.coordination import Board, Task


def _core_bytes(store: CardStore, card_id: str) -> bytes:
    """Read the immutable core file exactly as stored."""
    return (store.cards_dir / card_id / "core.json").read_bytes()


def _task_bytes(board: Board, card_id: str) -> bytes:
    """Read the immutable legacy task file exactly as stored."""
    return next(board.tasks_dir.glob(f"{card_id}-*.json")).read_bytes()


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

    view = next(
        view for view in board.get_task_views() if view.task.id == "criteria-view"
    )

    assert view.task.acceptance_criteria == ["folded criterion"]
    assert _core_bytes(store, "criteria-view") == before


@pytest.mark.parametrize("mode", [None, "1", "dual", "0", "off", "false", "no"])
def test_every_projection_folds_latest_criteria_amendment(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(
        Task(
            id="criteria-kill-switch",
            title="Card",
            acceptance_criteria=["birth criterion"],
        )
    )
    store = CardStore(tmp_path)
    task_before = _task_bytes(board, "criteria-kill-switch")
    core_before = _core_bytes(store, "criteria-kill-switch")
    store.append_event(
        "criteria-kill-switch",
        "amend_criteria",
        "reviewer",
        criteria=["first amendment"],
    )
    store.append_event(
        "criteria-kill-switch",
        "amend_criteria",
        "reviewer",
        criteria=["current one", "current two"],
    )

    if mode is None:
        monkeypatch.delenv("SKCOORD_CARD_STORE", raising=False)
    else:
        monkeypatch.setenv("SKCOORD_CARD_STORE", mode)
    projected = next(
        card
        for card in KanbanBoard(tmp_path).cards()
        if card.id == "criteria-kill-switch"
    )

    assert projected.acceptance_criteria == ["current one", "current two"]
    assert _task_bytes(board, "criteria-kill-switch") == task_before
    assert _core_bytes(store, "criteria-kill-switch") == core_before


def test_legacy_only_task_retains_birth_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.create_task(
        Task(
            id="criteria-legacy-only",
            title="Legacy-only card",
            acceptance_criteria=["birth criterion"],
        )
    )

    projected = next(
        view
        for view in board.get_task_views()
        if view.task.id == "criteria-legacy-only"
    )

    assert not (tmp_path / "cards" / "criteria-legacy-only").exists()
    assert projected.task.acceptance_criteria == ["birth criterion"]


def test_known_card_with_malformed_core_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-bad-core", title="Card"))
    core_path = tmp_path / "cards" / "criteria-bad-core" / "core.json"
    core_path.write_text("{malformed", encoding="utf-8")
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="core"):
        board.get_task_views()


def test_known_card_with_missing_core_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-missing-core", title="Card"))
    (tmp_path / "cards" / "criteria-missing-core" / "core.json").unlink()
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="core"):
        board.get_task_views()


def test_known_card_with_unreadable_core_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-unreadable-core", title="Card"))
    original_read = CardStore._read_regular_file_bytes

    def unreadable_core(directory_fd, name, label):
        if label == "CardStore core":
            raise OSError("injected unreadable core")
        return original_read(directory_fd, name, label)

    monkeypatch.setattr(
        CardStore, "_read_regular_file_bytes", staticmethod(unreadable_core)
    )
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="core"):
        board.get_task_views()


def test_known_card_with_malformed_event_json_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-bad-event", title="Card"))
    store = CardStore(tmp_path)
    store.append_event(
        "criteria-bad-event", "amend_criteria", "reviewer", criteria=["current"]
    )
    event_path = next(
        (tmp_path / "cards" / "criteria-bad-event" / "events").glob("*.jsonl")
    )
    event_path.write_text("{malformed\n", encoding="utf-8")
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="event"):
        board.get_task_views()


def test_known_card_with_unreadable_event_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-unreadable-event", title="Card"))
    CardStore(tmp_path).append_event(
        "criteria-unreadable-event",
        "amend_criteria",
        "reviewer",
        criteria=["current"],
    )
    original_read = CardStore._read_regular_file_bytes

    def unreadable_event(directory_fd, name, label):
        if label == "CardStore event source":
            raise OSError("injected unreadable event")
        return original_read(directory_fd, name, label)

    monkeypatch.setattr(
        CardStore, "_read_regular_file_bytes", staticmethod(unreadable_event)
    )
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="event"):
        board.get_task_views()


@pytest.mark.parametrize("criteria", [None, [], [""], ["valid", 7]])
def test_known_card_with_malformed_criteria_event_fails_closed(
    tmp_path, monkeypatch, criteria
):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(Task(id="criteria-bad-payload", title="Card"))
    CardStore(tmp_path).append_event(
        "criteria-bad-payload", "amend_criteria", "reviewer", criteria=criteria
    )
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")

    with pytest.raises(ValueError, match="criteria"):
        board.get_task_views()


def test_parity_reports_criteria_when_legacy_fold_is_bypassed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = Board(tmp_path)
    board.create_task(
        Task(
            id="criteria-parity",
            title="Card",
            acceptance_criteria=["birth criterion"],
        )
    )
    CardStore(tmp_path).append_event(
        "criteria-parity", "amend_criteria", "reviewer", criteria=["current criterion"]
    )

    def stale_criteria(home, card_id, birth_criteria=None, store=None):
        del home, card_id, store
        return list(birth_criteria or [])

    monkeypatch.setattr(card_store, "current_acceptance_criteria", stale_criteria)
    result = parity_check(tmp_path)
    mismatch = next(
        item for item in result["mismatches"] if item["id"] == "criteria-parity"
    )

    assert mismatch["diff"]["acceptance_criteria"] == [
        ["birth criterion"],
        ["current criterion"],
    ]


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
    exported = next(
        task for task in Board(tmp_path).load_tasks() if task.id == "criteria-export"
    )

    assert result["tasks_written"] == 1
    assert exported.acceptance_criteria == ["current criterion"]
    assert _core_bytes(store, "criteria-export") == before
