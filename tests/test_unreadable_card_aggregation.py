"""Malformed historical CardStore lines must not break aggregation."""

from __future__ import annotations

import json

from skcoord.card_store import CardCore, CardStore, task_views_from_store


def _store_with_one_malformed_event(home) -> CardStore:
    """Create two healthy cards and corrupt exactly one event stream."""
    store = CardStore(home)
    for card_id in ("readable-a", "unreadable-b", "readable-c"):
        store.create(CardCore(id=card_id, title=f"Card {card_id}"))

    store.append_event("unreadable-b", "note", "fixture", text="valid event")
    event_path = next((store.cards_dir / "unreadable-b" / "events").glob("*.jsonl"))
    valid_event = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
    assert isinstance(valid_event, dict)
    with event_path.open("a", encoding="utf-8") as stream:
        # This intentionally invalid line is the red fixture exercising the
        # strict reader boundary. Production appends remain serializer-built.
        stream.write("{malformed fixture\n")
    return store


def test_one_malformed_event_is_skipped_without_truncating_kanban(tmp_path, monkeypatch):
    """Kanban preserves the card and its valid history without rewriting evidence."""
    store = _store_with_one_malformed_event(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")

    card = store.fold("unreadable-b")
    assert card is not None and card.title == "Card unreadable-b"
    assert {item.id for item in store.list_cards()} == {
        "readable-a",
        "unreadable-b",
        "readable-c",
    }

    from skcoord.card import KanbanBoard

    cards = {card.id: card for card in KanbanBoard(tmp_path).cards()}
    assert set(cards) == {"readable-a", "unreadable-b", "readable-c"}
    assert cards["readable-a"].title == "Card readable-a"
    assert cards["readable-c"].title == "Card readable-c"
    assert cards["unreadable-b"].title == "Card unreadable-b"


def test_one_malformed_event_is_skipped_without_truncating_status(tmp_path):
    """Status aggregation preserves all task views and valid event history."""
    _store_with_one_malformed_event(tmp_path)

    views = {view.task.id: view for view in task_views_from_store(tmp_path)}
    assert set(views) == {"readable-a", "unreadable-b", "readable-c"}
    assert views["unreadable-b"].task.title == "Card unreadable-b"
