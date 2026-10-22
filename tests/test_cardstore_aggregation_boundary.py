"""A broken card stays visible without taking down multi-card readers."""

import hashlib

from skcoord.card import KanbanBoard
from skcoord.card_store import CardCore, CardStore, task_views_from_store


def test_malformed_event_is_one_explicit_unreadable_card(tmp_path):
    store = CardStore(tmp_path)
    for card_id in ("readable-1", "broken-1", "readable-2"):
        store.create(CardCore(id=card_id, title=card_id))

    event_path = tmp_path / "cards" / "broken-1" / "events" / "red-fixture@host.jsonl"
    event_path.parent.mkdir()
    event_path.write_bytes(b'{"ts":"2026-08-26T00:00:00Z","action":"move"}\n{malformed\n')
    frozen_event_bytes = event_path.read_bytes()
    frozen_event_hash = "631251338a740ac8e4bf6e2faee11c6e88bb4fa4d9468d6cd3cf55e2a06a702e"
    assert hashlib.sha256(frozen_event_bytes).hexdigest() == frozen_event_hash

    cards = CardStore(tmp_path).list_cards()
    assert [card.id for card in cards] == ["broken-1", "readable-1", "readable-2"]
    broken = cards[0]
    assert broken.title == "UNREADABLE"
    assert broken.description == "CardStore event source for broken-1 is malformed"
    assert broken.source == "cards/broken-1"
    assert broken.meta == {
        "unreadable": True,
        "source": "cards/broken-1",
        "reason": "CardStore event source for broken-1 is malformed",
    }

    kanban_cards = KanbanBoard(tmp_path).cards()
    assert [card.id for card in kanban_cards] == ["broken-1", "readable-1", "readable-2"]
    views = task_views_from_store(tmp_path)
    assert [view.task.id for view in views] == ["broken-1", "readable-1", "readable-2"]
    assert views[0].status.value == "blocked"
    assert views[0].task.title == "UNREADABLE"
    assert views[0].task.meta["reason"] == "CardStore event source for broken-1 is malformed"

    assert event_path.read_bytes() == frozen_event_bytes
    assert hashlib.sha256(event_path.read_bytes()).hexdigest() == frozen_event_hash
