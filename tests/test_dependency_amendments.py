"""Append-only dependency amendment coverage for coordination claim gates."""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from skcoord.card_store import CardStore, add_dependency, remove_dependency
from skcoord.coordination import Board, Task


@pytest.fixture(autouse=True)
def _store_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Exercise the default CardStore projection and clear lazy CLI imports."""
    loaded_modules = set(sys.modules)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    yield
    for module_name in set(sys.modules) - loaded_modules:
        if module_name == "skcapstone" or module_name.startswith("skcapstone."):
            sys.modules.pop(module_name, None)


def _seed(board: Board) -> None:
    board.create_task(Task(id="gate0001", title="independent review"))
    board.create_task(Task(id="work0001", title="implementation"))


def _core_bytes(home, card_id: str) -> bytes:
    return (CardStore(home).cards_dir / card_id / "core.json").read_bytes()


def test_added_dependency_blocks_claim_until_synthetic_gate_completion(
    tmp_path,
) -> None:
    """An appended review dependency is enforced at normal claim time."""
    board = Board(tmp_path)
    _seed(board)
    birth_core = _core_bytes(tmp_path, "work0001")

    assert add_dependency(
        tmp_path,
        "work0001",
        "gate0001",
        agent="reviewer",
        reason="independent review is a mandatory implementation gate",
    )
    assert not add_dependency(
        tmp_path,
        "work0001",
        "gate0001",
        agent="reviewer",
        reason="retry after uncertain transport result",
    )
    assert _core_bytes(tmp_path, "work0001") == birth_core

    with pytest.raises(ValueError, match="gate0001"):
        board.claim_task("implementer", "work0001")

    board.claim_task("reviewer", "gate0001")
    board.complete_task("reviewer", "gate0001")
    claimed = board.claim_task("implementer", "work0001")

    assert "work0001" in claimed.claimed_tasks
    events = CardStore(tmp_path)._read_events("work0001")
    additions = [event for event in events if event.get("action") == "add_dependency"]
    assert len(additions) == 1
    assert additions[0]["dependency"] == "gate0001"
    assert additions[0]["writer"] == "reviewer"
    assert (
        additions[0]["reason"]
        == "independent review is a mandatory implementation gate"
    )


def test_dependency_removal_is_append_only_reversible_and_survives_legacy_rollback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a gate is an attributed rollback, never a task/core rewrite."""
    board = Board(tmp_path)
    _seed(board)
    birth_core = _core_bytes(tmp_path, "work0001")
    assert add_dependency(
        tmp_path,
        "work0001",
        "gate0001",
        agent="reviewer",
        reason="apply mandatory review gate",
    )

    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    with pytest.raises(ValueError, match="gate0001"):
        board.claim_task("implementer", "work0001")

    assert remove_dependency(
        tmp_path,
        "work0001",
        "gate0001",
        agent="reviewer",
        reason="synthetic rollback verification only",
    )
    assert not remove_dependency(
        tmp_path,
        "work0001",
        "gate0001",
        agent="reviewer",
        reason="idempotent rollback retry",
    )
    assert _core_bytes(tmp_path, "work0001") == birth_core
    claimed = board.claim_task("implementer", "work0001")
    assert "work0001" in claimed.claimed_tasks
    events = CardStore(tmp_path)._read_events("work0001")
    assert [event["action"] for event in events if "dependency" in event] == [
        "add_dependency",
        "remove_dependency",
    ]


def test_dependency_amendment_rejects_unknown_or_self_referential_cards(
    tmp_path,
) -> None:
    """Dependency amendments fail closed before adding an impossible gate."""
    board = Board(tmp_path)
    _seed(board)

    with pytest.raises(ValueError, match="dependency missing0 not found"):
        add_dependency(tmp_path, "work0001", "missing0", reason="must fail closed")
    with pytest.raises(ValueError, match="cannot depend on itself"):
        add_dependency(tmp_path, "work0001", "work0001", reason="must fail closed")
