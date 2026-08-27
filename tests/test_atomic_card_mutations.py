"""Cross-process atomicity and projection-failure coverage."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import pytest

from skcoord.card_store import CardStore, add_dependency
from skcoord.coordination import Board, Task


def _add(home: str) -> bool:
    return add_dependency(
        home, "a1e20001", "b2e20002", agent="worker", reason="concurrency test"
    )


def _release(home: str) -> bool:
    return Board(home).release_claim("probe", "a1e30001", actor="repair")


def test_eight_identical_additions_append_one_event(tmp_path) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="a1e20001", title="target"))
    board.create_task(Task(id="b2e20002", title="gate"))
    with ProcessPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_add, [str(tmp_path)] * 8))
    assert results.count(True) == 1
    events = [
        e
        for e in CardStore(tmp_path)._read_events("a1e20001")
        if e.get("action") == "add_dependency"
    ]
    assert len(events) == 1


def test_eight_identical_releases_append_one_transition(tmp_path) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="a1e30001", title="target"))
    board.claim_task("probe", "a1e30001")
    with ProcessPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_release, [str(tmp_path)] * 8))
    assert results.count(True) == 1
    events = [
        e
        for e in CardStore(tmp_path)._read_events("a1e30001")
        if e.get("released_owner") == "probe"
    ]
    assert [e["action"] for e in events] == ["release_claim"]


def test_release_store_failure_restores_legacy_projection(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="a1e40001", title="target"))
    board.claim_task("probe", "a1e40001")
    monkeypatch.setattr(
        board,
        "_mirror_card_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("store failed")),
    )
    with pytest.raises(OSError, match="store failed"):
        board.release_claim("probe", "a1e40001", actor="repair")
    assert "a1e40001" in board.load_agent("probe").claimed_tasks


def test_release_legacy_failure_writes_no_store_transition(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="a1e50001", title="target"))
    board.claim_task("probe", "a1e50001")
    original = board.save_agent
    calls = {"count": 0}

    def fail_release(agent):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("legacy failed")
        return original(agent)

    monkeypatch.setattr(board, "save_agent", fail_release)
    with pytest.raises(OSError, match="legacy failed"):
        board.release_claim("probe", "a1e50001", actor="repair")
    assert not [
        e
        for e in CardStore(tmp_path)._read_events("a1e50001")
        if e.get("released_owner")
    ]
