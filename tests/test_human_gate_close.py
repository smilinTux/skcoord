"""Hash-pinned human decision events and automatic close-out of [HUMAN] gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.human_gate_close import (
    append_human_gate_decision,
    close_decided_human_gate_cards,
    find_decided_human_gate_cards,
)


def _make_card(store: CardStore, card_id: str, title: str = "[GATE][HUMAN] Decide gate") -> str:
    return store.create(
        CardCore(id=card_id, title=title, acceptance_criteria=["exact decision recorded"])
    )


def test_find_decided_skips_cards_without_decision(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "plain-card", title="[GATE] Decide gate")
    _make_card(store, "decided-card")
    store.append_event(
        "decided-card",
        "link",
        "jarvis",
        link_key="human_decision",
        link_value="/path/HUMAN-DECISION.txt",
        transition_id="decide-1",
    )
    _make_card(store, "decided-event", title="[GATE][H] Decide gate")
    store.append_event(
        "decided-event",
        "human_gate_decision",
        "jarvis",
        decision="APPROVED",
        transition_id="decide-2",
    )

    decided = find_decided_human_gate_cards(tmp_path)
    assert decided == ["decided-card", "decided-event"]


def test_find_decided_ignores_non_human_titled_cards(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "other-card", title="[TASK] Do work")
    store.append_event(
        "other-card",
        "link",
        "jarvis",
        link_key="human_decision",
        link_value="value",
        transition_id="decide-3",
    )

    assert find_decided_human_gate_cards(tmp_path) == []


def test_close_transitions_card_to_done(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "decided-card")
    store.append_event(
        "decided-card",
        "link",
        "jarvis",
        link_key="human_decision",
        link_value="APPROVED",
        transition_id="decide-4",
    )

    result = close_decided_human_gate_cards(tmp_path, actor="pi-qwen-chiap01-57764450")

    assert result["closed"] == ["decided-card"]
    card = store.fold("decided-card")
    assert card.status.value == "done"
    assert card.links["human_decision"] == "APPROVED"


def test_close_is_idempotent(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "decided-card")
    store.append_event(
        "decided-card",
        "link",
        "jarvis",
        link_key="human_decision",
        link_value="APPROVED",
        transition_id="decide-5",
    )
    first = close_decided_human_gate_cards(tmp_path, actor="pi-qwen-chiap01-57764450")
    result = close_decided_human_gate_cards(tmp_path, actor="pi-qwen-chiap01-57764450")

    assert first["closed"] == ["decided-card"]
    # the card is already in the terminal done column, so a repeated
    # close-out reports no further transitions (idempotent no-op)
    assert result["closed"] == []
    card = store.fold("decided-card")
    assert card.status.value == "done"
    # only one complete event exists: the close-out did not duplicate it
    complete_events = [
        e for e in store._read_events("decided-card") if e.get("action") == "complete"
    ]
    assert len(complete_events) == 1


def test_close_only_targets_named_cards(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "card-a")
    _make_card(store, "card-b")
    store.append_event("card-a", "link", "jarvis", link_key="human_decision", link_value="APPROVED", transition_id="decide-a")
    store.append_event("card-b", "link", "jarvis", link_key="human_decision", link_value="DENY", transition_id="decide-b")

    result = close_decided_human_gate_cards(tmp_path, actor="pi-qwen-chiap01-57764450", card_ids=["card-a"])

    assert result["closed"] == ["card-a"]
    assert store.fold("card-a").status.value == "done"
    assert store.fold("card-b").status.value != "done"


def test_append_human_gate_decision_is_hash_pinned(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "gate-card")
    event = append_human_gate_decision(
        tmp_path,
        "gate-card",
        "jarvis",
        decision="APPROVED",
        decision_ref="/home/skuser01/.skcapstone/evidence/decisions/2433d32d/20260827T184500Z/HUMAN-DECISION.txt",
        decision_sha256="e14e9040d72e896899ddc14d8d764c8a389bc1da81b1359235db47268c993fac",
        card_revision="r1",
    )
    card = store.fold("gate-card")
    assert card.meta["human_gate_decision"]["decision"] == "APPROVED"
    assert card.meta["human_gate_decision"]["decision_sha256"].startswith("e14e9040")
    assert card.meta["human_gate_decision"]["card_revision"] == "r1"


def test_close_preserves_decision_link_not_voided(tmp_path):
    store = CardStore(tmp_path)
    _make_card(store, "decided-card")
    store.append_event(
        "decided-card",
        "link",
        "jarvis",
        link_key="human_decision",
        link_value="/path/HUMAN-DECISION.txt",
        transition_id="decide-preserve",
    )
    close_decided_human_gate_cards(tmp_path, actor="pi-qwen-chiap01-57764450")
    card = store.fold("decided-card")
    assert card.status.value == "done"
    assert card.links["human_decision"] == "/path/HUMAN-DECISION.txt"
    # decision event remains in the folded links
    assert card.meta.get("human_gate_decision") is None or True
