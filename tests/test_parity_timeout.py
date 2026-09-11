"""Bounded-snapshot regression tests for parity_check().

Fleet finding 5 (2026-09-05): parity_check() had no deadline and read mutable
sources at different times, causing caller timeouts and false drift. These
tests pin the repair: fail-closed deadline, single snapshot window with all
legacy reads before the store read, and snapshot metadata in the result.
"""

from __future__ import annotations

import time

import skcoord.card_store as card_store
from skcoord.card import KanbanBoard
from skcoord.card_store import CardCore, CardStore, parity_check
from skcoord.coordination import Board, Task


def _seed_home(tmp_path):
    """One card present in both the legacy projection and the store."""
    board = Board(tmp_path)
    board.create_task(Task(id="snap01", title="Snapshot test card"))
    store = CardStore(tmp_path)
    store.create(CardCore(id="snap01", title="Snapshot test card"))
    return board, store


class _AdvancingClock:
    """Fake time module whose every read jumps forward past any deadline."""

    def monotonic(self):
        self.t += 10.0
        return self.t

    def monotonic_ns(self):
        return int(self.monotonic() * 1_000_000_000)

    def __init__(self):
        self.t = 1000.0


def test_zero_timeout_fails_closed_before_any_read(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        KanbanBoard, "cards", lambda self, **kw: calls.append("legacy") or []
    )
    monkeypatch.setattr(
        CardStore, "list_cards", lambda self, **kw: calls.append("store") or []
    )
    monkeypatch.setattr(
        card_store, "_legacy_status_ids", lambda home, deadline=None: set()
    )
    result = parity_check(tmp_path, timeout=0.0)
    assert result["outcome"] == "snapshot_timeout"
    assert result["actionable"] is False
    assert calls == [], "deadline must trip before any source read"


def test_exceeded_deadline_raises_parity_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    # First monotonic() read (snapshot start) stays under the deadline; every
    # later read has advanced past it, so the legacy status-id walk trips.
    clock = _AdvancingClock()
    monkeypatch.setattr(card_store, "time", clock)
    result = parity_check(tmp_path, timeout=5.0)
    assert result["outcome"] == "snapshot_timeout"
    assert result["actionable"] is False


def test_synchronous_legacy_projection_is_interrupted_at_deadline(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)

    def _slow_cards(self, **kwargs):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(KanbanBoard, "cards", _slow_cards)
    started = time.monotonic()
    result = parity_check(tmp_path, timeout=0.1)
    elapsed = time.monotonic() - started

    assert result["outcome"] == "snapshot_timeout"
    assert result["actionable"] is False
    assert elapsed < 0.5


def test_synchronous_snapshot_copy_is_interrupted_at_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    inventory = card_store._parity_inventory(tmp_path, None)
    original_read_bytes = card_store.Path.read_bytes

    def _slow_read_bytes(path):
        time.sleep(1.0)
        return original_read_bytes(path)

    monkeypatch.setattr(card_store.Path, "read_bytes", _slow_read_bytes)
    monkeypatch.setattr(card_store, "_parity_inventory", lambda *_args: inventory)
    started = time.monotonic()
    result = parity_check(tmp_path, timeout=0.1)
    elapsed = time.monotonic() - started

    assert result["outcome"] == "snapshot_timeout"
    assert result["actionable"] is False
    assert elapsed < 0.5


def test_timeout_does_not_wait_for_partial_snapshot_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    cleanup = []
    inventories = iter((("a", []), ("b", [])))
    monkeypatch.setattr(
        card_store,
        "_parity_inventory",
        lambda *_args: next(inventories),
    )
    monkeypatch.setattr(
        card_store,
        "_defer_snapshot_cleanup",
        lambda snapshot: cleanup.append(snapshot),
    )
    monkeypatch.setattr(card_store.shutil, "rmtree", lambda *_args: time.sleep(1.0))
    started = time.monotonic()
    result = parity_check(tmp_path, timeout=0.1)

    assert result["outcome"] == "snapshot_timeout"
    assert time.monotonic() - started < 0.5
    assert len(cleanup) == 1
    assert cleanup[0].name.startswith(".skcoord-parity-")


def test_result_carries_snapshot_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    result = parity_check(tmp_path, timeout=30.0)
    assert result["outcome"] == "healthy"
    snap = result["snapshot"]
    assert snap["timeout_seconds"] == 30.0
    assert snap["pre_inventory_hash"] == snap["post_inventory_hash"]
    assert snap["post_inventory_hash"] == snap["snapshot_inventory_hash"]
    assert snap["legacy_read_ns"] <= snap["store_read_ns"]
    assert snap["span_ns"] == snap["store_read_ns"] - snap["legacy_read_ns"]
    assert snap["span_ns"] >= 0


def test_snapshot_is_shared_by_legacy_and_store_consumers(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    roots = []
    legacy_cards = KanbanBoard.cards
    store_cards = CardStore.list_cards

    def _cards(self, **kw):
        roots.append(self.home)
        return legacy_cards(self, **kw)

    def _list(self, **kw):
        roots.append(self.home)
        return store_cards(self, **kw)

    monkeypatch.setattr(KanbanBoard, "cards", _cards)
    monkeypatch.setattr(CardStore, "list_cards", _list)

    result = parity_check(tmp_path)
    assert result["outcome"] == "healthy"
    assert len(roots) == 2
    assert roots[0] == roots[1]
    assert roots[0] != tmp_path
    assert not roots[0].exists(), "temporary snapshot must be removed after consumption"


def test_source_drift_during_copy_is_non_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    task_path = next((tmp_path / "coordination" / "tasks").glob("*.json"))
    original_read_bytes = card_store.Path.read_bytes

    def _read_bytes(path):
        payload = original_read_bytes(path)
        if path == task_path:
            path.write_bytes(payload + b" ")
        return payload

    monkeypatch.setattr(card_store.Path, "read_bytes", _read_bytes)
    result = parity_check(tmp_path)
    assert result["outcome"] == "snapshot_unstable"
    assert result["actionable"] is False
    assert result["mismatches"] == []


def test_reconcile_refuses_unsafe_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        card_store,
        "parity_check",
        lambda home: {
            "outcome": "snapshot_unstable",
            "actionable": False,
            "mismatches": [{"id": "snap01", "diff": {"owner": ["x", None]}}],
        },
    )
    appended = []
    monkeypatch.setattr(
        CardStore,
        "append_event",
        lambda self, *args, **kwargs: appended.append(args),
    )
    result = card_store.reconcile_from_legacy(tmp_path, dry_run=False)
    assert result == {
        "fixed": 0,
        "would_fix": 0,
        "skipped_uncomplete": [],
        "outcome": "snapshot_unstable",
        "actionable": False,
    }
    assert appended == []


def test_legacy_reads_precede_store_read(tmp_path, monkeypatch):
    """All legacy-side reads happen inside the window, before the store fold.

    The pre-repair code read the store fold and only THEN re-read the legacy
    tasks directory via _legacy_status_ids(), the widest timing skew. This
    pins the back-to-back order: legacy cards, legacy status ids, store.
    """
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    order: list[str] = []

    orig_cards = KanbanBoard.cards
    orig_status = card_store._legacy_status_ids
    orig_list = CardStore.list_cards

    def _cards(self, **kw):
        order.append("legacy_cards")
        return orig_cards(self, **kw)

    def _status(home, deadline=None):
        order.append("legacy_status_ids")
        return orig_status(home, deadline=deadline)

    def _list(self, **kw):
        order.append("store_fold")
        return orig_list(self, **kw)

    monkeypatch.setattr(KanbanBoard, "cards", _cards)
    monkeypatch.setattr(card_store, "_legacy_status_ids", _status)
    monkeypatch.setattr(CardStore, "list_cards", _list)

    parity_check(tmp_path)
    assert order == ["legacy_cards", "legacy_status_ids", "store_fold"]


def test_timeout_none_disables_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    _seed_home(tmp_path)
    # A clock far past any plausible deadline must not raise when disabled.
    clock = _AdvancingClock()
    monkeypatch.setattr(card_store, "time", clock)
    result = parity_check(tmp_path, timeout=None)
    assert result["snapshot"]["timeout_seconds"] is None


def test_parity_timeout_is_timeout_error():
    assert issubclass(card_store.ParityTimeout, TimeoutError)
    assert not issubclass(card_store._SnapshotTimeout, Exception)
