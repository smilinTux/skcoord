"""Regression tests for legacy field absence in parity checks."""

from __future__ import annotations

import json

from skcoord.card_store import CardCore, CardStore, parity_check
from skcoord.coordination import Board, Task


def test_complete_store_chain_with_statusless_legacy_record_matches(
    tmp_path, monkeypatch
) -> None:
    """A legacy birth record without status must not contradict completion."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    board.create_task(Task(id="legacy01", title="Statusless birth record"))

    legacy_path = next(board.tasks_dir.glob("legacy01-*.json"))
    legacy_record = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert "status" not in legacy_record
    assert "done_at" not in legacy_record

    store = CardStore(tmp_path)
    store.create(CardCore(id="legacy01", title="Statusless birth record"))
    store.append_event("legacy01", "claim", "worker", owner="worker")
    store.append_event("legacy01", "move", "worker", column="review")
    store.append_event("legacy01", "complete", "worker")

    result = parity_check(tmp_path, open_drift_threshold=0)

    assert result["mismatches"] == []
    assert result["matched"] == result["checked"] == 1
    assert result["open_legacy"] == result["open_store"] == 0
    assert result["open_alert"] is False
