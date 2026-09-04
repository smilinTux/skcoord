"""Regressions for terminal void behavior across structural write paths."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skcoord.card import CardEvent, CardEventLog, KanbanBoard
from skcoord.card_store import CardCore, CardStore, export_to_legacy
from skcoord.coordination import Board


def _voided_store(home, card_id: str = "voidterm1") -> CardStore:
    store = CardStore(home)
    store.create(CardCore(id=card_id, kind="task", title="Void terminal"))
    store.append_event(card_id, "void", "chef", reason="duplicate")
    return store


@pytest.mark.parametrize(
    ("action", "payload"),
    [("move", {"column": "doing"}), ("reopen", {"column": "ready"})],
)
def test_direct_cardstore_structural_append_rejects_voided_card(
    tmp_path, action, payload
) -> None:
    store = _voided_store(tmp_path)

    with pytest.raises(ValueError, match="void is a terminal decision"):
        store.append_event("voidterm1", action, "bypass", **payload)

    card = store.fold("voidterm1")
    assert card is not None
    assert card.archived is True
    assert card.owner is None


def test_void_after_complete_is_terminal_and_emits_audit_warnings(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="voiddone1", kind="task", title="Void completed"))
    complete = store.append_event("voiddone1", "complete", "finisher")

    void = store.append_event("voiddone1", "void", "governor", reason="superseded")

    card = store.fold("voiddone1")
    assert card is not None
    assert card.status.value != "done"
    assert card.archived is True
    assert card.meta["voided"] is True
    events = store._read_events("voiddone1")
    assert [event["action"] for event in events] == [
        "complete",
        "void",
        "void_after_complete",
        "card_voided_after_completion",
    ]
    assert events[0]["event_id"] == complete["event_id"]
    assert events[1]["event_id"] == void["event_id"]
    assert events[2]["void_event_id"] == void["event_id"]
    assert events[3]["void_event_id"] == void["event_id"]
    assert events[3]["warning"] == "void_after_complete"


def test_void_before_complete_remains_terminal_in_historical_audit(tmp_path) -> None:
    store = _voided_store(tmp_path)
    complete = {
        "action": "complete",
        "writer": "historical-bypass",
        "ts": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "seq": 0,
    }
    store._legacy_cache = {"voidterm1": [complete]}

    card = store.fold("voidterm1")

    assert card is not None
    assert card.status.value != "done"
    assert card.archived is True
    assert [event["action"] for event in store._read_events("voidterm1")] == ["void"]
    assert store._legacy_events("voidterm1") == [complete]


def test_fold_ignores_historical_store_resurrection_after_void(tmp_path) -> None:
    store = _voided_store(tmp_path)
    move = {
        "action": "move",
        "column": "doing",
        "writer": "historical-bypass",
        "ts": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "seq": 0,
    }
    store._legacy_cache = {"voidterm1": [move]}

    card = store.fold("voidterm1")

    assert card is not None
    assert card.archived is True
    assert card.owner is None
    assert card.meta["voided"] is True


def test_legacy_overlay_cannot_append_move_for_voided_card(tmp_path) -> None:
    _voided_store(tmp_path)

    with pytest.raises(ValueError, match="void is a terminal decision"):
        CardEventLog(tmp_path).append(
            CardEvent(
                card_id="voidterm1",
                action="move",
                column="doing",
                writer="legacy-bypass",
            )
        )


def test_legacy_projection_ignores_overlay_that_sorts_after_void(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="voidterm1", kind="task", title="Void terminal"))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="voidterm1",
            action="move",
            column="doing",
            writer="legacy-bypass",
            ts=future,
        )
    )
    store.append_event("voidterm1", "void", "chef", reason="duplicate")

    cards = KanbanBoard(tmp_path).cards(include_archived=True)
    card = next(item for item in cards if item.id == "voidterm1")

    assert card.archived is True
    assert card.owner is None


def test_export_does_not_assign_voided_card(tmp_path) -> None:
    store = _voided_store(tmp_path)
    store._legacy_cache = {
        "voidterm1": [
            {
                "action": "move",
                "column": "doing",
                "ts": "9999-01-01T00:00:00+00:00",
                "writer": "legacy-bypass",
                "seq": 0,
            },
            {
                "action": "assign",
                "owner": "legacy-owner",
                "ts": "9999-01-01T00:00:01+00:00",
                "writer": "legacy-bypass",
                "seq": 1,
            },
        ]
    }

    export_to_legacy(tmp_path)

    assert all(
        "voidterm1" not in agent.claimed_tasks
        and "voidterm1" not in agent.completed_tasks
        and agent.current_task != "voidterm1"
        for agent in Board(tmp_path).load_agents()
    )
