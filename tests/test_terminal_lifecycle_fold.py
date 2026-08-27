"""Terminal card lifecycle events are sticky and later lifecycle writes are inert."""

from skcoord.card import Column
import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.lifecycle import transition_task


def _store(tmp_path, card_id: str) -> CardStore:
    store = CardStore(tmp_path)
    store.create(CardCore(id=card_id, title="Terminal fixture"))
    return store


def test_complete_ignores_later_assign_claim_unassign_move_and_void(tmp_path) -> None:
    store = _store(tmp_path, "terminal1")
    store.append_event("terminal1", "complete", "worker")
    store.append_event("terminal1", "assign", "stray", owner="stray")
    store.append_event("terminal1", "claim", "stray", owner="stray")
    store.append_event("terminal1", "unassign", "stray")
    store.append_event("terminal1", "move", "stray", column="doing")
    store.append_event("terminal1", "void", "stray", reason="too late")

    card = store.fold("terminal1")

    assert card is not None
    assert card.status == Column.DONE
    assert card.owner is None
    assert card.meta["terminal_action"] == "complete"
    assert [event["action"] for event in card.meta["ignored_terminal_events"]] == [
        "assign",
        "claim",
        "unassign",
        "move",
        "void",
    ]


def test_move_command_path_refuses_terminal_card_without_writing(tmp_path) -> None:
    store = _store(tmp_path, "terminal3")
    store.append_event("terminal3", "complete", "worker")
    event_count = len(store._read_events("terminal3"))

    with pytest.raises(ValueError, match="terminal card terminal3.*cannot be moved"):
        transition_task(tmp_path, task_id="terminal3", column="doing", actor="stray")

    assert len(store._read_events("terminal3")) == event_count
    assert store.fold("terminal3").status == Column.DONE


def test_void_is_terminal_and_ignores_later_lifecycle_events(tmp_path) -> None:
    store = _store(tmp_path, "terminal2")
    store.append_event("terminal2", "void", "worker", reason="created by mistake")
    store.append_event("terminal2", "assign", "stray", owner="stray")
    store.append_event("terminal2", "claim", "stray", owner="stray")
    store.append_event("terminal2", "unassign", "stray")

    card = store.fold("terminal2")

    assert card is not None
    assert card.status == Column.DONE
    assert card.owner is None
    assert card.meta["terminal_action"] == "void"
    assert card.meta["void_reason"] == "created by mistake"
