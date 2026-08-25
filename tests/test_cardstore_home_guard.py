"""Regression coverage for shared-home and CardStore orphan guards."""

from __future__ import annotations

import pytest

import skcoord.card as card_module
import skcoord.card_store as card_store_module
from skcoord.card import CardEvent, CardEventLog, Column
from skcoord.card_store import CardStore, import_from_legacy
from skcoord.coordination import AgentFile, Board, Task, TaskPriority


def test_coordination_subdirectory_is_rejected_before_nested_write(tmp_path) -> None:
    wrong_home = tmp_path / "coordination"
    wrong_home.mkdir()

    with pytest.raises(ValueError, match="shared root"):
        CardEventLog(wrong_home).append(CardEvent(card_id="home0001", action="link"))

    assert not (wrong_home / "coordination").exists()
    assert not (wrong_home / "cards").exists()


def test_coordination_home_guard_is_sensitive(tmp_path, monkeypatch) -> None:
    wrong_home = tmp_path / "coordination"
    wrong_home.mkdir()
    monkeypatch.setattr(card_module, "validate_shared_home", lambda home: home)

    CardEventLog(wrong_home).append(CardEvent(card_id="home0001", action="link"))

    assert (wrong_home / "coordination" / "card_events").is_dir()


@pytest.mark.parametrize("action", ["describe", "link", "amend_criteria"])
def test_cardstore_event_requires_foldable_core(tmp_path, action) -> None:
    store = CardStore(tmp_path)

    with pytest.raises(ValueError, match="no foldable core"):
        store.append_event("orphan0001", action, "reviewer")

    assert not (tmp_path / "cards" / "orphan0001").exists()


def test_cardstore_core_guard_is_sensitive(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(CardStore, "_require_foldable_core", lambda self, card_id: None)

    CardStore(tmp_path).append_event("orphan0001", "link", "reviewer")

    assert (tmp_path / "cards" / "orphan0001" / "events").is_dir()


def _legacy_task(home, monkeypatch, task_id: str) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(home)
    board.create_task(
        Task(
            id=task_id,
            title="Preserve every field",
            description="Legacy description",
            priority=TaskPriority.HIGH,
            tags=["security", "identity"],
            created_by="legacy-author",
            acceptance_criteria=["criterion one", "criterion two"],
            dependencies=["gate0001"],
            meta={"amendment": {"reason": "reviewed"}},
        )
    )
    board.save_agent(
        AgentFile(
            agent="legacy-owner",
            current_task=task_id,
            claimed_tasks=[task_id],
        )
    )


def _assert_preserved_card(card) -> None:
    assert card is not None
    assert card.title == "Preserve every field"
    assert card.description == "Legacy description"
    assert card.acceptance_criteria == ["criterion one", "criterion two"]
    assert card.dependencies == ["gate0001"]
    assert card.labels == ["security", "identity"]
    assert card.priority == "high"
    assert card.status == Column.DOING
    assert card.owner == "legacy-owner"
    assert card.meta["amendment"] == {"reason": "reviewed"}


def test_legacy_import_preserves_all_reviewed_fields(tmp_path, monkeypatch) -> None:
    _legacy_task(tmp_path, monkeypatch, "import0001")

    result = import_from_legacy(tmp_path)

    assert result["imported"] == 1
    _assert_preserved_card(CardStore(tmp_path).fold("import0001"))


def test_legacy_import_criteria_check_is_sensitive(tmp_path, monkeypatch) -> None:
    _legacy_task(tmp_path, monkeypatch, "import0002")
    original_core = card_store_module.CardCore

    def drop_criteria(**payload):
        payload["acceptance_criteria"] = []
        return original_core(**payload)

    monkeypatch.setattr(card_store_module, "CardCore", drop_criteria)
    import_from_legacy(tmp_path)

    with pytest.raises(AssertionError):
        _assert_preserved_card(CardStore(tmp_path).fold("import0002"))
