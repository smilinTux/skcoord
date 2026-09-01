"""Agent projection reconciliation against the event-sourced kanban lifecycle."""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

import skcoord.lifecycle as lifecycle_module
from skcoord import audit_lifecycle as public_audit_lifecycle
from skcoord.card import KanbanBoard
from skcoord.card_store import CardStore
from skcoord.coordination import AgentFile, Board, Task
from skcoord.lifecycle import (
    LifecycleConflictError,
    audit_lifecycle,
    repair_lifecycle,
    transition_task,
)


@pytest.fixture(autouse=True)
def _store_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the card store and isolate lazy SKCapstone imports per test."""
    loaded_modules = set(sys.modules)
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    yield
    for module_name in set(sys.modules) - loaded_modules:
        if module_name == "skcapstone" or module_name.startswith("skcapstone."):
            sys.modules.pop(module_name, None)


def _task(board: Board, task_id: str = "task0001") -> Task:
    task = Task(id=task_id, title="Lifecycle task", created_by="tester")
    board.create_task(task)
    return task


def _move(store: CardStore, task_id: str, column: str, writer: str = "jarvis") -> None:
    store.append_event(task_id, "move", writer, column=column)


def test_review_preserves_owner_without_active_execution(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")

    before = audit_lifecycle(tmp_path)
    assert {issue.code for issue in before.issues} == {"review_reported_active"}

    receipt = repair_lifecycle(tmp_path, actor="operator")

    agent = board.load_agent("jarvis")
    assert agent is not None
    assert agent.current_task is None
    assert agent.claimed_tasks == [task.id]
    assert receipt.after.clean is True
    assert receipt.receipt_path.exists()


def test_done_clears_claim_and_records_historical_completion(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "done")

    receipt = repair_lifecycle(tmp_path, actor="operator")

    agent = board.load_agent("jarvis")
    assert agent is not None
    assert agent.current_task is None
    assert task.id not in agent.claimed_tasks
    assert agent.completed_tasks.count(task.id) == 1
    assert receipt.after.clean is True


def test_reopen_removes_completion_and_restores_current_owner(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    store = CardStore(tmp_path)
    _move(store, task.id, "done")
    repair_lifecycle(tmp_path, actor="operator")

    _move(store, task.id, "doing")
    repair_lifecycle(tmp_path, actor="operator")

    agent = board.load_agent("jarvis")
    assert agent is not None
    assert agent.current_task == task.id
    assert agent.claimed_tasks == [task.id]
    assert task.id not in agent.completed_tasks


def test_duplicate_complete_and_repair_are_idempotent(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    board.complete_task("jarvis", task.id)
    board.complete_task("jarvis", task.id)

    first = repair_lifecycle(tmp_path, actor="operator")
    second = repair_lifecycle(tmp_path, actor="operator")

    agent = board.load_agent("jarvis")
    assert agent is not None
    assert agent.completed_tasks.count(task.id) == 1
    assert first.after.clean is True
    assert second.actions == ()


def test_stale_orphan_claim_is_released_but_history_is_preserved(tmp_path) -> None:
    board = Board(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    agent = AgentFile(
        agent="jarvis",
        last_seen=stale,
        current_task="missing1",
        claimed_tasks=["missing1"],
        completed_tasks=["historical1"],
    )
    board.ensure_dirs()
    (board.agents_dir / "jarvis.json").write_text(
        json.dumps(agent.model_dump(), indent=2) + "\n", encoding="utf-8"
    )

    receipt = repair_lifecycle(tmp_path, actor="operator", stale_after_seconds=60)

    repaired = board.load_agent("jarvis")
    assert repaired is not None
    assert repaired.last_seen == stale
    assert repaired.current_task is None
    assert repaired.claimed_tasks == []
    assert repaired.completed_tasks == ["historical1"]
    assert receipt.after.clean is True


def test_fresh_orphan_claim_requires_human_resolution(tmp_path) -> None:
    board = Board(tmp_path)
    board.save_agent(
        AgentFile(agent="jarvis", current_task="missing1", claimed_tasks=["missing1"])
    )

    with pytest.raises(LifecycleConflictError, match="active orphan claim"):
        repair_lifecycle(tmp_path, actor="operator", stale_after_seconds=3600)


def test_two_active_agents_claiming_same_card_is_a_conflict(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.save_agent(AgentFile(agent="jarvis", current_task=task.id, claimed_tasks=[task.id]))
    board.save_agent(AgentFile(agent="opus", current_task=task.id, claimed_tasks=[task.id]))

    with pytest.raises(LifecycleConflictError, match="multiple active owners"):
        repair_lifecycle(tmp_path, actor="operator")


def test_stale_non_owner_claim_is_released_without_restamping_liveness(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("owner", task.id)
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    other = AgentFile(
        agent="other",
        state="active",
        last_seen=stale,
        current_task=task.id,
        claimed_tasks=[task.id],
    )
    (board.agents_dir / "other.json").write_text(
        json.dumps(other.model_dump(), indent=2) + "\n", encoding="utf-8"
    )

    receipt = repair_lifecycle(tmp_path, actor="operator", stale_after_seconds=60)

    repaired = board.load_agent("other")
    assert repaired is not None
    assert repaired.last_seen == stale
    assert repaired.current_task is None
    assert repaired.claimed_tasks == []
    assert receipt.after.clean is True


def test_active_non_owner_claim_requires_human_resolution(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("owner", task.id)
    board.save_agent(AgentFile(agent="other", current_task=task.id, claimed_tasks=[task.id]))

    with pytest.raises(LifecycleConflictError, match="active non-owner claim"):
        repair_lifecycle(tmp_path, actor="operator", stale_after_seconds=3600)


def test_restart_readback_remains_clean(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    repair_lifecycle(tmp_path, actor="operator")

    restarted = audit_lifecycle(tmp_path)

    assert restarted.clean is True
    assert restarted.card_count == 1
    assert public_audit_lifecycle(tmp_path).clean is True


def test_transition_task_moves_and_reconciles_before_return(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)

    receipt = transition_task(tmp_path, task_id=task.id, column="review", actor="operator")

    card = CardStore(tmp_path).fold(task.id)
    agent = board.load_agent("jarvis")
    assert card is not None and card.status.value == "review"
    assert agent is not None and agent.current_task is None
    assert task.id in agent.claimed_tasks
    assert receipt.after.clean is True


def test_transition_rejects_voided_card_before_moving(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    store = CardStore(tmp_path)
    store.append_event(task.id, "void", "chef", reason="Superseded by successor")
    store.append_event(task.id, "archive", "chef")

    with pytest.raises(ValueError, match="voided and cannot be moved"):
        transition_task(tmp_path, task_id=task.id, column="ready", actor="coord-move")

    events = store._read_events(task.id)
    assert [event["action"] for event in events][-2:] == ["void", "archive"]
    assert store.fold(task.id).archived is True
    assert events[-2]["reason"] == "Superseded by successor"


def test_transition_rejects_active_conflict_before_moving(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("owner", task.id)
    board.save_agent(AgentFile(agent="other", current_task=task.id, claimed_tasks=[task.id]))

    with pytest.raises(LifecycleConflictError, match="active non-owner claim"):
        transition_task(tmp_path, task_id=task.id, column="review", actor="operator")

    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.status.value == "doing"


def test_transition_supports_legacy_only_rollback_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)

    receipt = transition_task(tmp_path, task_id=task.id, column="review", actor="operator")

    card = next(item for item in KanbanBoard(tmp_path).cards() if item.id == task.id)
    agent = board.load_agent("jarvis")
    assert card.status.value == "review"
    assert agent is not None and agent.current_task is None
    assert receipt.after.clean is True


def test_transition_compensates_when_projection_write_fails(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    original = lifecycle_module._repair_lifecycle_unlocked
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic projection failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module, "_repair_lifecycle_unlocked", fail_once)

    with pytest.raises(OSError, match="synthetic projection failure"):
        transition_task(tmp_path, task_id=task.id, column="review", actor="operator")

    card = CardStore(tmp_path).fold(task.id)
    agent = board.load_agent("jarvis")
    assert card is not None and card.status.value == "doing"
    assert agent is not None and agent.current_task == task.id


def test_transition_compensates_when_store_mirror_fails(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    original = lifecycle_module.mirror_coord_move
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic mirror failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle_module, "mirror_coord_move", fail_once)

    with pytest.raises(OSError, match="synthetic mirror failure"):
        transition_task(tmp_path, task_id=task.id, column="review", actor="operator")

    card = CardStore(tmp_path).fold(task.id)
    agent = board.load_agent("jarvis")
    assert card is not None and card.status.value == "doing"
    assert agent is not None and agent.current_task == task.id


def test_repair_rejects_untrusted_agent_payload_identity_and_cannot_escape(tmp_path) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel": true}\n', encoding="utf-8")
    payload = AgentFile(agent="safe-agent", claimed_tasks=["missing"]).model_dump()
    payload["agent"] = str(outside.with_suffix(""))
    (board.agents_dir / "poison.json").write_text(json.dumps(payload), encoding="utf-8")

    receipt = repair_lifecycle(tmp_path, actor="operator", stale_after_seconds=0)

    assert receipt.after.clean is True
    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert not outside.with_suffix(".json.json").exists()


def test_agent_payload_identity_must_match_filename(tmp_path) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    payload = AgentFile(agent="victim").model_dump()
    (board.agents_dir / "poison.json").write_text(json.dumps(payload), encoding="utf-8")

    assert board.load_agents() == []


def test_agent_projection_directory_symlink_is_rejected(tmp_path) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    board.agents_dir.rmdir()
    board.agents_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not be a symlink"):
        board.save_agent(AgentFile(agent="jarvis"))

    assert list(outside.iterdir()) == []


def test_repair_restores_projection_when_receipt_append_fails(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    projection = board.agents_dir / "jarvis.json"
    before = projection.read_bytes()

    def fail_receipt(*_args, **_kwargs):
        raise OSError("synthetic receipt failure")

    monkeypatch.setattr(lifecycle_module, "_append_receipt", fail_receipt)
    with pytest.raises(OSError, match="synthetic receipt failure"):
        repair_lifecycle(tmp_path, actor="operator")

    assert projection.read_bytes() == before
    assert not (tmp_path / "coordination" / "reconciliation").exists()
    assert audit_lifecycle(tmp_path).clean is False


def test_repair_receipt_symlink_cannot_redirect_append(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    projection = board.agents_dir / "jarvis.json"
    before = projection.read_bytes()
    outside = tmp_path / "outside-receipt.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    receipts = tmp_path / "coordination" / "reconciliation"
    receipts.mkdir()
    (receipts / f"operator@{socket.gethostname()}.jsonl").symlink_to(outside)

    with pytest.raises(LifecycleConflictError, match="receipt source is unsafe"):
        repair_lifecycle(tmp_path, actor="operator")

    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert projection.read_bytes() == before


def test_repair_receipt_hardlink_cannot_redirect_append(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    projection = board.agents_dir / "jarvis.json"
    before = projection.read_bytes()
    outside = tmp_path / "outside-receipt.jsonl"
    outside.write_text("sentinel\n", encoding="utf-8")
    receipts = tmp_path / "coordination" / "reconciliation"
    receipts.mkdir()
    (receipts / f"operator@{socket.gethostname()}.jsonl").hardlink_to(outside)

    with pytest.raises(LifecycleConflictError, match="receipt source is unsafe"):
        repair_lifecycle(tmp_path, actor="operator")

    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert projection.read_bytes() == before


def test_repair_journals_intent_before_committed_projection_receipt(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")

    receipt = repair_lifecycle(tmp_path, actor="operator")
    events = [
        json.loads(line) for line in receipt.receipt_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [event["phase"] for event in events] == ["intent", "committed"]
    assert len({event["receipt_id"] for event in events}) == 1
    assert events[0]["projection_agents"] == ["jarvis"]


def test_repair_recovers_unpaired_intent_after_process_death(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    original_append = lifecycle_module._append_receipt

    def stop_before_commit(home, actor, payload):
        if payload.get("phase") == "committed":
            raise SystemExit("synthetic process death")
        return original_append(home, actor, payload)

    monkeypatch.setattr(lifecycle_module, "_append_receipt", stop_before_commit)
    with pytest.raises(SystemExit, match="synthetic process death"):
        repair_lifecycle(tmp_path, actor="operator")
    monkeypatch.setattr(lifecycle_module, "_append_receipt", original_append)

    assert audit_lifecycle(tmp_path).clean is True
    repair_lifecycle(tmp_path, actor="operator")
    receipt_path = (
        tmp_path / "coordination" / "reconciliation" / f"operator@{socket.gethostname()}.jsonl"
    )
    events = [json.loads(line) for line in receipt_path.read_text().splitlines()]
    first_id = events[0]["receipt_id"]
    first_phases = [event["phase"] for event in events if event["receipt_id"] == first_id]
    recovered = next(event for event in events if event["phase"] == "recovered")

    assert first_phases == ["intent", "recovered"]
    assert recovered["recovered_by"] != first_id
    assert recovered["after"]["clean"] is True


def test_scoped_repair_does_not_recover_unrelated_global_intent(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    first = _task(board, "task0001")
    second = _task(board, "task0002")
    board.claim_task("jarvis", first.id)
    board.claim_task("opus", second.id)
    _move(CardStore(tmp_path), first.id, "review")
    original_append = lifecycle_module._append_receipt

    def stop_after_intent(home, actor, payload):
        path = original_append(home, actor, payload)
        if payload.get("phase") == "intent":
            raise SystemExit("synthetic process death")
        return path

    monkeypatch.setattr(lifecycle_module, "_append_receipt", stop_after_intent)
    with pytest.raises(SystemExit, match="synthetic process death"):
        repair_lifecycle(tmp_path, actor="operator", task_ids={first.id})
    monkeypatch.setattr(lifecycle_module, "_append_receipt", original_append)

    receipt_path = (
        tmp_path / "coordination" / "reconciliation" / f"operator@{socket.gethostname()}.jsonl"
    )
    abandoned_id = json.loads(receipt_path.read_text().splitlines()[0])["receipt_id"]
    repair_lifecycle(tmp_path, actor="operator", task_ids={second.id})
    scoped_events = [json.loads(line) for line in receipt_path.read_text().splitlines()]

    assert audit_lifecycle(tmp_path).clean is False
    assert [event["phase"] for event in scoped_events if event["receipt_id"] == abandoned_id] == [
        "intent"
    ]

    repair_lifecycle(tmp_path, actor="operator")
    full_events = [json.loads(line) for line in receipt_path.read_text().splitlines()]
    assert audit_lifecycle(tmp_path).clean is True
    assert [event["phase"] for event in full_events if event["receipt_id"] == abandoned_id] == [
        "intent",
        "recovered",
    ]


def test_phase_less_legacy_receipt_is_treated_as_terminal(tmp_path) -> None:
    board = Board(tmp_path)
    task = _task(board)
    board.claim_task("jarvis", task.id)
    _move(CardStore(tmp_path), task.id, "review")
    receipts = tmp_path / "coordination" / "reconciliation"
    receipts.mkdir()
    receipt_path = receipts / f"operator@{socket.gethostname()}.jsonl"
    receipt_path.write_text(
        json.dumps({"receipt_id": "legacy-receipt", "actor": "operator"}) + "\n",
        encoding="utf-8",
    )

    receipt = repair_lifecycle(tmp_path, actor="operator")

    assert receipt.after.clean is True
    events = [json.loads(line) for line in receipt_path.read_text().splitlines()]
    assert events[0]["receipt_id"] == "legacy-receipt"
    assert "phase" not in events[0]
