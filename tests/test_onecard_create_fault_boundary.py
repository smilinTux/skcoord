"""Regression: one malformed card must not freeze all board writes (card 33375183).

On 2026-08-31 a single malformed card event log blocked EVERY card creation
across the whole board (17 review cards failed with "CardStore event source
for <id> is malformed"). The documented one-card fault boundary
(``list_cards(degrade_unreadable=True)``) exists for read aggregation but the
create/governance path inherited the all-or-nothing strict read. This file
pins:

- a governed card create (the ``[REVIEW]`` path, which scans every card)
  succeeds while one unrelated card is unreadable, and
- a coord mirror create_task succeeds under the same condition, and
- the unreadable card is still surfaced, not silently skipped, and
- strict governance callers (parity/export style) still raise.
"""

from __future__ import annotations

import json

import pytest

from skcoord.card_store import CardCore, CardStore, export_to_legacy, parity_check


def _store_with_one_malformed_event(home) -> CardStore:
    """Build a store where exactly one card's event log is malformed."""
    store = CardStore(home)
    for card_id in ("readable-a", "unreadable-b", "readable-c"):
        store.create(CardCore(id=card_id, title=f"Card {card_id}"))

    store.append_event("unreadable-b", "note", "fixture", text="valid event")
    event_path = next((store.cards_dir / "unreadable-b" / "events").glob("*.jsonl"))
    with event_path.open("a", encoding="utf-8") as stream:
        stream.write("{malformed fixture\n")
    return store


def test_governed_create_survives_unrelated_malformed_card(tmp_path, monkeypatch):
    """Creating a [REVIEW] card must not require successfully reading another card."""
    store = _store_with_one_malformed_event(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")

    # The incident: 17 governed review creations all failed. After the fix,
    # the governed scan uses the one-card fault boundary.
    core = CardCore(
        id="review-r1",
        title="[REVIEW] verify readable-a",
        initial_labels=["parent-readable-a"],
    )
    created = store.create(core)
    assert created == "review-r1"

    # The malformed card is still present and surfaced.
    cards = store.list_cards(include_archived=True, degrade_unreadable=True)
    by_id = {card.id: card for card in cards}
    assert "unreadable-b" in by_id
    assert by_id["unreadable-b"].meta.get("unreadable") is True
    assert "UNREADABLE" in by_id["unreadable-b"].title


def test_mirrored_coord_create_survives_unrelated_malformed_card(tmp_path, monkeypatch):
    """Board.create_task (the 2026-08-31 incident path) succeeds."""
    _store_with_one_malformed_event(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")

    from skcoord.coordination import Board, Task, TaskPriority

    board = Board(tmp_path)
    task = Task(id="fresh-z", title="Card fresh-z", priority=TaskPriority.MEDIUM, tags=[])
    path = board.create_task(task)
    assert path.exists()


def test_governed_create_still_rejects_live_duplicate_when_parent_readable(tmp_path, monkeypatch):
    """Terminal/unreadable parents are tolerated; live non-terminal duplicates still fail closed."""
    store = _store_with_one_malformed_event(tmp_path)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")

    live_review = CardCore(
        id="review-live",
        title="[REVIEW] verify readable-a",
        initial_labels=["parent-readable-a"],
    )
    store.create(live_review)

    duplicate = CardCore(
        id="review-dup",
        title="[REVIEW] verify readable-a",
        initial_labels=["parent-readable-a"],
    )
    with pytest.raises(ValueError, match="Refusing live review duplicate"):
        store.create(duplicate)


def test_strict_governance_callers_still_fail_closed(tmp_path):
    """Parity and export are documented all-or-nothing; a malformed card must still raise there."""
    _store_with_one_malformed_event(tmp_path)

    with pytest.raises(ValueError, match="unreadable-b.*malformed"):
        parity_check(tmp_path)

    with pytest.raises(ValueError, match="unreadable-b.*malformed"):
        export_to_legacy(tmp_path, dry_run=True)
