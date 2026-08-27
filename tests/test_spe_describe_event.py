"""SPE P3.1: ``describe`` is a folded event, so title/description are not frozen.

Card ``be2e849a`` (epic ``373a33ca``). Before this, ``title`` and ``description``
were the only card fields still read straight off the write-once ``core.json``
(``card_store.fold``), while priority, swimlane, labels, links and status were
all folded from events. That made a wording fix impossible without superseding
the card id.

``describe`` closes that gap on both sides of the fold:

- the store's own per-writer event log (``CardStore.append_event``), and
- the sanctioned legacy overlay (``card_events/*.jsonl``), which
  ``load_legacy_mutations`` merges into the same stream.

``core.json`` stays write-once throughout, so there is no write-conflict
regression: the edit is an append, attributed to its writer and reversible by
appending again.
"""

from __future__ import annotations

import json

from skcoord.card import CardEvent, CardEventLog, Column, KanbanBoard, fold_overlay
from skcoord.card_store import CardCore, CardStore, mirror_coord_describe
from skcoord.coordination import Board, Task


def _core_on_disk(store: CardStore, card_id: str) -> dict:
    return json.loads(
        (store.cards_dir / card_id / "core.json").read_text(encoding="utf-8")
    )


def test_describe_event_updates_folded_description(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d1", title="Card", description="original wording"))
    store.append_event("d1", "describe", "lumina", description="tightened wording")
    assert store.fold("d1").description == "tightened wording"


def test_describe_event_updates_folded_title(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d2", title="typo in titel", description="body"))
    store.append_event("d2", "describe", "lumina", title="typo in title")
    card = store.fold("d2")
    assert card.title == "typo in title"
    assert card.description == "body"  # untouched field stays at the core value


def test_describe_leaves_core_json_write_once(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d3", title="Card", description="original wording"))
    before = _core_on_disk(store, "d3")
    store.append_event("d3", "describe", "lumina", title="New", description="New body")
    assert _core_on_disk(store, "d3") == before
    assert before["title"] == "Card"


def test_describe_can_clear_a_description(tmp_path):
    """An explicit empty string clears; an omitted field is left alone."""
    store = CardStore(tmp_path)
    store.create(CardCore(id="d4", title="Card", description="too long by half"))
    store.append_event("d4", "describe", "lumina", description="")
    assert store.fold("d4").description == ""


def test_describe_last_write_wins_in_fold_order(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d5", title="Card", description="v0"))
    store.append_event("d5", "describe", "lumina", description="v1")
    store.append_event("d5", "describe", "lumina", description="v2")
    assert store.fold("d5").description == "v2"


def test_describe_is_reversible_by_appending(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d6", title="Card", description="birth wording"))
    store.append_event("d6", "describe", "lumina", description="good edit")
    store.append_event("d6", "describe", "lumina", description="bad edit")
    store.append_event("d6", "describe", "lumina", description="good edit")  # the undo
    assert store.fold("d6").description == "good edit"


def test_describe_event_carries_writer_attribution(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d7", title="Card"))
    store.append_event("d7", "describe", "lumina", description="edited")
    events = store._read_events("d7")
    assert [e["action"] for e in events] == ["describe"]
    ev = events[0]
    assert ev["writer"] == "lumina"
    assert ev["node"] and ev["ts"] and ev["event_id"]


def test_legacy_overlay_describe_folds_into_the_store(tmp_path):
    """A describe written through the kanban overlay reaches the store fold."""
    store = CardStore(tmp_path)
    store.create(CardCore(id="d8", title="Card", description="original"))
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="d8",
            action="describe",
            description="from the overlay",
            writer="lumina",
        )
    )
    assert store.fold("d8").description == "from the overlay"


def test_fold_overlay_collects_describe_patch():
    events = [
        CardEvent(
            card_id="d9",
            action="describe",
            description="one",
            writer="a",
            ts="2026-01-01",
        ),
        CardEvent(
            card_id="d9", action="describe", title="two", writer="a", ts="2026-01-02"
        ),
    ]
    patch = fold_overlay(events)["d9"]
    assert patch["description"] == "one"
    assert patch["title"] == "two"


def test_kanban_board_applies_describe_overlay_to_legacy_projection(
    tmp_path, monkeypatch
):
    """The legacy (store-disabled) projection honours describe too."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    Board(tmp_path).create_task(Task(id="d10", title="Card", description="original"))
    CardStore(tmp_path).create(CardCore(id="d10", title="Card", description="original"))
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="d10", action="describe", description="patched", writer="lumina"
        )
    )
    card = next(c for c in KanbanBoard(tmp_path).cards() if c.id == "d10")
    assert card.description == "patched"
    assert card.title == "Card"


def test_mirror_coord_describe_omits_untouched_fields(tmp_path):
    """The CLI-facing mirror must not write a null title over a real one."""
    store = CardStore(tmp_path)
    store.create(CardCore(id="d12", title="Card", description="original"))
    mirror_coord_describe(tmp_path, "d12", "lumina", description="edited")
    ev = store._read_events("d12")[0]
    assert "title" not in ev
    assert ev["description"] == "edited"
    assert store.fold("d12").title == "Card"


def test_describe_does_not_disturb_other_folded_state(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="d11", title="Card", description="original"))
    store.append_event("d11", "move", "lumina", column="doing")
    store.append_event("d11", "add_label", "lumina", label="spe")
    store.append_event("d11", "describe", "lumina", description="edited")
    card = store.fold("d11")
    assert card.description == "edited"
    assert card.status == Column.DOING
    assert card.labels == ["spe"]
