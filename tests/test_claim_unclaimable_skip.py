"""A voided/absent/archived card must be a typed refusal, never a crash.

Observed live 2026-09-18 on the builder node ziowk01-wsl: card 59553966 was
voided and replaced by 59550966 as part of a source-binding repair while the
sknoded node worker still held the old card in its dispatch queue. The worker
called ``Board.claim_task``, ``_claim_task`` raised a bare
``ValueError: Task 59553966 not found``, and that exception unwound the whole
daemon main loop (``sknoded.service`` exited 1, losing 18h of process state).

The message was also a lie: the card was never absent. ``void`` sets
``archived=True`` in the fold, and ``get_task_views`` reads
``list_cards(include_archived=False)``, so a voided card simply falls out of
the projection the claim path searches. These tests pin the real reason onto
the refusal so an operator reading a log line is not sent hunting for a card
that is sitting right there on disk.
"""

from __future__ import annotations

import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.coordination import Board, Task, TaskUnclaimable


def _task(board: Board, task_id: str, **kw) -> Task:
    task = Task(id=task_id, title=f"Task {task_id}", created_by="tester", **kw)
    board.create_task(task)
    return task


def _card(home, card_id: str = "59553966") -> CardStore:
    store = CardStore(home)
    store.create(CardCore(id=card_id, kind="task", title="Source-bound builder card"))
    return store


def test_claiming_a_voided_card_refuses_with_the_void_reason(tmp_path) -> None:
    store = _card(tmp_path)
    store.append_event("59553966", "void", "chef", reason="source-binding repair")

    with pytest.raises(TaskUnclaimable) as excinfo:
        Board(tmp_path).claim_task("pi-builder-standby-node-ziowk01", "59553966")

    assert excinfo.value.reason == "voided"
    assert excinfo.value.task_id == "59553966"
    assert excinfo.value.terminal is True
    # The card is present and foldable; "not found" would misdirect the reader.
    assert "not found" not in str(excinfo.value)
    assert "voided" in str(excinfo.value)


def test_claiming_an_archived_card_refuses_with_the_archive_reason(tmp_path) -> None:
    store = _card(tmp_path, "59550111")
    store.append_event("59550111", "archive", "chef")

    with pytest.raises(TaskUnclaimable) as excinfo:
        Board(tmp_path).claim_task("jarvis", "59550111")

    assert excinfo.value.reason == "archived"
    assert excinfo.value.terminal is True


def test_claiming_a_genuinely_absent_card_keeps_its_existing_wording(tmp_path) -> None:
    """Absent is refused BEFORE the claim projection, at the card lock.

    A card with no directory cannot be locked, so card_mutation_lock refuses it
    from inside claim_task's ExitStack with "has no foldable core" -- the same
    daemon-killing bare-ValueError shape as the voided crash. It is now typed
    too, but the message is deliberately unchanged: skcapstone's
    tests/test_coordination.py::test_claim_nonexistent_task pins that exact
    string with an anchored regex, and this change is about the type of the
    refusal, not its wording.
    """
    with pytest.raises(TaskUnclaimable) as excinfo:
        Board(tmp_path).claim_task("jarvis", "deadbeef")

    assert excinfo.value.reason == "absent"
    assert excinfo.value.terminal is True
    assert str(excinfo.value) == "CardStore card deadbeef has no foldable core"


def test_refusal_stays_a_valueerror_for_every_pre_existing_caller(tmp_path) -> None:
    """``coord claim``, the MCP tool, auction and spawner all catch ValueError.

    TaskUnclaimable narrows the type for callers that must survive one bad
    card; it must not change what the existing ``except ValueError`` sites see.
    """
    store = _card(tmp_path, "59550222")
    store.append_event("59550222", "void", "chef")

    with pytest.raises(ValueError, match="59550222"):
        Board(tmp_path).claim_task("jarvis", "59550222")


def test_an_owned_card_is_a_non_terminal_refusal(tmp_path) -> None:
    """Already claimed by someone else: refuse, but do not park it forever."""
    board = Board(tmp_path)
    task = _task(board, "owned001")
    board.claim_task("jarvis", task.id)

    with pytest.raises(TaskUnclaimable) as excinfo:
        board.claim_task("lumina", task.id)

    assert excinfo.value.reason == "owned"
    assert excinfo.value.terminal is False


def test_incomplete_dependencies_are_a_non_terminal_refusal(tmp_path) -> None:
    board = Board(tmp_path)
    dep = _task(board, "depopen1")
    task = _task(board, "blocked1", dependencies=[dep.id])

    with pytest.raises(TaskUnclaimable) as excinfo:
        board.claim_task("jarvis", task.id)

    assert excinfo.value.reason == "dependencies"
    assert excinfo.value.terminal is False
    assert dep.id in str(excinfo.value)


def test_a_corrupt_card_store_is_not_downgraded_to_a_skip(tmp_path, monkeypatch) -> None:
    """Only an individual absent/voided/terminal card degrades to a refusal.

    A store that cannot be read at all is infrastructure failure, and the
    daemon handlers deliberately do not catch it: it must stay loud rather
    than turning every card on the node into a silent skip.
    """
    _card(tmp_path, "59550333")

    def _boom(self, card_id):
        raise OSError("cards/ is unreadable")

    monkeypatch.setattr(CardStore, "fold", _boom)

    with pytest.raises(OSError, match="unreadable"):
        Board(tmp_path).claim_task("jarvis", "59550333")
