"""Canonical CardStore supersession folding and query behavior."""

from __future__ import annotations

import pytest

from skcoord.card import Column, SupersessionState
from skcoord.card_store import CardCore, CardStore
from skcoord.coordination import Board, Task, TaskStatus


@pytest.fixture
def incident_cards(tmp_path):
    """Create the two stopped gates and their exact canonical successors."""
    store = CardStore(tmp_path)
    for card_id in ("13cb2667", "cc531c74", "7ddca485", "ec1c4b6b"):
        store.create(CardCore(id=card_id, title=f"Card {card_id}"))
    return store


def _supersede(store: CardStore, card_id: str, superseding_card_id: str, writer: str):
    return store.append_event(
        card_id,
        "supersede",
        writer,
        superseding_card_id=superseding_card_id,
    )


def test_canonical_supersession_projects_exact_successor(incident_cards) -> None:
    """13cb2667 exposes enforced evidence and cannot look ordinarily open."""
    event = _supersede(incident_cards, "13cb2667", "cc531c74", "owner")

    card = incident_cards.fold("13cb2667")

    assert card.supersession_state == SupersessionState.SUPERSEDED
    assert card.superseded_by == "cc531c74"
    assert card.supersession_evidence == (event["event_id"],)
    assert card.status == Column.BACKLOG
    view = next(v for v in Board(incident_cards.home).get_task_views() if v.task.id == card.id)
    assert view.status == TaskStatus.SUPERSEDED
    assert view.task.meta["superseded_by"] == "cc531c74"


def test_free_form_link_never_creates_supersession(incident_cards) -> None:
    """7ddca485's historical superseded_by link remains non-authoritative."""
    incident_cards.append_event(
        "7ddca485",
        "link",
        "annotator",
        link_key="superseded_by",
        link_value="ec1c4b6b",
    )

    card = incident_cards.fold("7ddca485")

    assert card.links["superseded_by"] == "ec1c4b6b"
    assert card.supersession_state == SupersessionState.ACTIVE
    assert card.superseded_by is None
    assert card.status == Column.BACKLOG


def test_superseded_marker_without_enforced_evidence_fails_closed(incident_cards) -> None:
    incident_cards.append_event("13cb2667", "add_label", "annotator", label="superseded")
    incident_cards.append_event(
        "13cb2667",
        "link",
        "annotator",
        link_key="superseded_by",
        link_value="cc531c74",
    )

    card = incident_cards.fold("13cb2667")

    assert card.supersession_state == SupersessionState.INDETERMINATE
    assert card.superseded_by is None
    assert card.status == Column.BACKLOG


def test_conflicting_enforced_evidence_fails_closed(incident_cards) -> None:
    _supersede(incident_cards, "7ddca485", "ec1c4b6b", "owner-a")
    _supersede(incident_cards, "7ddca485", "cc531c74", "owner-b")

    card = incident_cards.fold("7ddca485")

    assert card.supersession_state == SupersessionState.INDETERMINATE
    assert card.superseded_by is None
    assert card.status == Column.BACKLOG


@pytest.mark.parametrize("successor", ["", " padded ", "7ddca485", "missing"])
def test_malformed_self_or_absent_successor_fails_closed(incident_cards, successor: str) -> None:
    _supersede(incident_cards, "7ddca485", successor, "owner")

    card = incident_cards.fold("7ddca485")

    assert card.supersession_state == SupersessionState.INDETERMINATE
    assert card.superseded_by is None
    assert card.status == Column.BACKLOG


def test_repeated_identical_enforced_evidence_converges(incident_cards) -> None:
    first = _supersede(incident_cards, "13cb2667", "cc531c74", "owner-a")
    second = _supersede(incident_cards, "13cb2667", "cc531c74", "owner-b")

    card = incident_cards.fold("13cb2667")

    assert card.supersession_state == SupersessionState.SUPERSEDED
    assert card.superseded_by == "cc531c74"
    assert set(card.supersession_evidence) == {first["event_id"], second["event_id"]}


def test_legacy_marker_only_claim_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.create_task(Task(id="13cb2667", title="Historical gate", tags=["superseded"]))

    with pytest.raises(ValueError, match="supersession is indeterminate"):
        board.claim_task("worker", "13cb2667")

    assert board.load_agent("worker") is None


def test_claim_refuses_superseded_and_indeterminate_cards(tmp_path) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="13cb2667", title="Historical gate"))
    board.create_task(Task(id="cc531c74", title="Current gate"))
    board.create_task(Task(id="7ddca485", title="Ambiguous gate"))
    store = CardStore(tmp_path)
    _supersede(store, "13cb2667", "cc531c74", "owner")
    store.append_event("7ddca485", "add_label", "owner", label="superseded")

    with pytest.raises(ValueError, match="superseded by cc531c74"):
        board.claim_task("worker", "13cb2667")
    with pytest.raises(ValueError, match="supersession is indeterminate"):
        board.claim_task("worker", "7ddca485")

    assert board.load_agent("worker") is None
