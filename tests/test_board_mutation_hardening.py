"""Strict board-mutation locking, recovery, and projection convergence tests."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Event, Process

import pytest

import skcoord.coordination as coordination_module
from skcoord.card import CardEvent, CardEventLog, Column, Kind
from skcoord.card_store import CardCore, CardStore, card_mutation_lock
from skcoord.coordination import Board, Task, _board_mutation_lock
from skcoord.lifecycle import audit_lifecycle, transition_task


def _release_one(home: str, task_id: str) -> bool:
    return Board(home).release_claim("owner", task_id, actor="repair")


def _force_claim(home: str, task_id: str) -> str:
    Board(home).claim_task("owner", task_id, force=True)
    return "claimed"


def _complete(home: str, task_id: str) -> str:
    Board(home).complete_task("owner", task_id)
    return "completed"


def _transition_review(home: str, task_id: str) -> str:
    transition_task(home, task_id=task_id, column="review", actor="operator")
    return "review"


def _run_race_item(item: tuple[str, str, str]):
    action, home, task_id = item
    operations = {
        "complete": _complete,
        "force_claim": _force_claim,
        "release": _release_one,
        "transition_review": _transition_review,
    }
    return operations[action](home, task_id)


def _hold_board_lock(home: str, ready: Event) -> None:
    with _board_mutation_lock(home):
        ready.set()
        time.sleep(0.4)


def _recovery_records(home) -> list[dict]:
    records: list[dict] = []
    for path in sorted((home / "coordination" / "recovery").glob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def _create_claimed(board: Board, task_id: str = "hard0001") -> Task:
    task = Task(id=task_id, title=task_id)
    board.create_task(task)
    board.claim_task("owner", task.id)
    return task


def test_same_owner_different_card_releases_keep_both_projection_updates(tmp_path) -> None:
    board = Board(tmp_path)
    first = _create_claimed(board, "hard0001")
    second = _create_claimed(board, "hard0002")

    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(_release_one, [str(tmp_path), str(tmp_path)], [first.id, second.id])
        )

    assert results == [True, True]
    agent = board.load_agent("owner")
    assert agent is not None and agent.claimed_tasks == [] and agent.current_task is None
    assert audit_lifecycle(tmp_path).clean is True


def test_forced_claim_release_interleaving_preserves_one_consistent_projection(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)

    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _run_race_item,
                [("force_claim", str(tmp_path), task.id), ("release", str(tmp_path), task.id)],
            )
        )

    assert set(outcomes) == {"claimed", True}
    assert audit_lifecycle(tmp_path).clean is True


def test_complete_release_interleaving_converges_to_done(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)

    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _run_race_item,
                [("complete", str(tmp_path), task.id), ("release", str(tmp_path), task.id)],
            )
        )

    assert "completed" in outcomes
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.status == Column.DONE and card.owner is None
    assert audit_lifecycle(tmp_path).clean is True


def test_lifecycle_transition_release_interleaving_uses_shared_board_lock(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)

    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _run_race_item,
                [
                    ("transition_review", str(tmp_path), task.id),
                    ("release", str(tmp_path), task.id),
                ],
            )
        )

    assert set(outcomes) == {"review", True}
    assert audit_lifecycle(tmp_path).clean is True


def test_board_lock_times_out_when_another_process_holds_it(tmp_path) -> None:
    ready = Event()
    process = Process(target=_hold_board_lock, args=(str(tmp_path), ready))
    process.start()
    assert ready.wait(timeout=2)
    try:
        with pytest.raises(TimeoutError, match="board mutation"):
            with _board_mutation_lock(tmp_path, timeout_seconds=0.05):
                pass
    finally:
        process.join(timeout=2)
    assert process.exitcode == 0


@pytest.mark.parametrize("task_id", ["../escape", "bad\x00id", "bad\ncontrol", "x" * 129])
def test_invalid_identifier_is_rejected_before_any_lock_path_is_opened(tmp_path, task_id) -> None:
    home = tmp_path / "new-home"
    with pytest.raises(ValueError, match="non-path"):
        Board(home).claim_task("owner", task_id)
    assert not (home / "coordination").exists()


@pytest.mark.parametrize("agent", ["../owner", "owner\x00", "owner\n", "x" * 129])
def test_invalid_projection_owner_is_rejected_before_lock_or_compensation_io(
    tmp_path, agent
) -> None:
    home = tmp_path / "new-home"
    with pytest.raises(ValueError, match="canonical filename stem"):
        Board(home).claim_task(agent, "hard0001")
    assert not (home / "coordination").exists()


def test_board_and_card_lock_symlinks_and_hardlinks_are_rejected(tmp_path) -> None:
    home = tmp_path / "home"
    locks = home / "coordination" / "locks"
    outside = tmp_path / "outside"
    home.mkdir()
    (home / "coordination").mkdir()
    outside.mkdir()
    locks.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="lock directory"):
        with _board_mutation_lock(home):
            pass
    with pytest.raises(ValueError, match="lock directory"):
        with card_mutation_lock(home, "hard0001"):
            pass

    locks.unlink()
    locks.mkdir()
    board_lock = locks / "board-mutations.lock"
    board_lock.symlink_to(outside / "board-target")
    with pytest.raises(ValueError, match="board mutation lock path"):
        with _board_mutation_lock(home):
            pass
    board_lock.unlink()
    card_lock = locks / f"{hashlib.sha256(b'hard0001').hexdigest()}.lock"
    card_lock.write_text("lock", encoding="utf-8")
    os.link(card_lock, outside / "linked-lock")
    with pytest.raises(ValueError, match="single-link"):
        with card_mutation_lock(home, "hard0001"):
            pass


def test_agent_parent_and_recovery_symlinks_never_redirect_mutation_io(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    board = Board(home)
    task = Task(id="hard0001", title="symlink guard")
    board.create_task(task)
    board.agents_dir.rmdir()
    board.agents_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        board.claim_task("owner", task.id)
    assert list(outside.iterdir()) == []

    board.agents_dir.unlink()
    board.ensure_dirs()
    board.claim_task("owner", task.id)
    recovery = board.coord_dir / "recovery"
    recovery.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        board,
        "_mirror_card_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("store failure")),
    )
    with pytest.raises(RuntimeError, match="record could not be persisted"):
        board.release_claim("owner", task.id, actor="repair")
    assert task.id in board.load_agent("owner").claimed_tasks
    assert list(outside.iterdir()) == []


def test_hardlinked_agent_projection_is_rejected_before_release_mutates(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    projection = board.agent_projection_path("owner")
    os.link(projection, projection.with_name("owner-copy.json"))

    with pytest.raises(ValueError, match="single-link"):
        board.release_claim("owner", task.id, actor="repair")
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.owner == "owner"


def test_release_event_requires_matching_owner_and_claim_revision(tmp_path) -> None:
    board = Board(tmp_path)
    task = Task(id="hard0001", title="revision")
    board.create_task(task)
    store = CardStore(tmp_path)
    store.append_event(task.id, "claim", "first", owner="first", claim_revision="rev-first")
    store.append_event(
        task.id,
        "release_claim",
        "first",
        released_owner="first",
        expected_claim_revision="rev-first",
    )
    store.append_event(task.id, "claim", "second", owner="second", claim_revision="rev-second")
    store.append_event(
        task.id,
        "release_claim",
        "first",
        released_owner="first",
        expected_claim_revision="rev-first",
    )

    card = store.fold(task.id)
    assert card is not None and card.owner == "second" and card.status == Column.DOING
    assert card.meta["release_conflicts"][0]["reason"] == "claim precondition did not match"


def test_release_write_then_error_keeps_durable_transition_as_success(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    projection = board.agent_projection_path("owner")
    original_append = CardStore.append_event

    def append_then_raise(self, card_id, action, agent, **payload):
        original_append(self, card_id, action, agent, **payload)
        if action == "release_claim":
            raise OSError("event bytes were written before failure")

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    assert board.release_claim("owner", task.id, actor="repair") is True
    assert task.id not in board.load_agent("owner").claimed_tasks
    assert projection.read_bytes() != b""
    assert _recovery_records(tmp_path) == []

    monkeypatch.setattr(CardStore, "append_event", original_append)
    assert board.release_claim("owner", task.id, actor="repair") is False
    releases = [
        event
        for event in CardStore(tmp_path)._read_events(task.id)
        if event.get("action") == "release_claim"
    ]
    assert len(releases) == 1
    assert audit_lifecycle(tmp_path).clean is True


def test_second_cross_host_claim_conflict_blocks_completion(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    CardStore(tmp_path).append_event(
        task.id,
        "claim",
        "remote-owner",
        owner="remote-owner",
        claim_revision="remote-revision",
        transition_id="remote-claim",
    )

    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.owner == "owner"
    assert card.meta["claim_conflicts"][0]["owner"] == "remote-owner"
    with pytest.raises(ValueError, match="concurrent claim conflicts"):
        board.complete_task("owner", task.id)


@pytest.mark.parametrize("operation", ["claim", "complete"])
def test_claim_and_complete_write_then_error_keep_applied_transition_as_success(
    tmp_path, monkeypatch, operation
) -> None:
    board = Board(tmp_path)
    task = Task(id="hard0001", title="write then error")
    board.create_task(task)
    if operation == "complete":
        board.claim_task("owner", task.id)
    original_append = CardStore.append_event

    def append_then_raise(self, card_id, action, agent, **payload):
        original_append(self, card_id, action, agent, **payload)
        if action == operation:
            raise OSError("event bytes were written before failure")

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    if operation == "claim":
        board.claim_task("owner", task.id)
        agent = board.load_agent("owner")
        assert agent is not None and task.id in agent.claimed_tasks
        expected_status = Column.DOING
        expected_owner = "owner"
    else:
        board.complete_task("owner", task.id)
        agent = board.load_agent("owner")
        assert agent is not None and task.id in agent.completed_tasks
        expected_status = Column.DONE
        expected_owner = None
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.status == expected_status and card.owner == expected_owner
    assert _recovery_records(tmp_path) == []


def test_remove_label_mirror_failure_restores_task_and_propagates(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = Task(id="hard0001", title="label", tags=["autopilot-staged"])
    path = board.create_task(task)
    original_bytes = path.read_bytes()
    original_mirror = board._mirror_card_store

    def fail_remove(op, **kwargs):
        if op == "remove_label":
            raise OSError("remove label mirror failed")
        return original_mirror(op, **kwargs)

    monkeypatch.setattr(board, "_mirror_card_store", fail_remove)
    with pytest.raises(OSError, match="remove label mirror failed"):
        board.update_task(task.id, remove_tags=["autopilot-staged"])
    assert path.read_bytes() == original_bytes
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and "autopilot-staged" in card.labels
    assert any(record["phase"] == "legacy_task_restored" for record in _recovery_records(tmp_path))


def test_remove_label_write_then_error_keeps_durable_label_transition(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    task = Task(id="hard0001", title="label", tags=["autopilot-staged"])
    path = board.create_task(task)
    original_append = CardStore.append_event

    def append_then_raise(self, card_id, action, agent, **payload):
        original_append(self, card_id, action, agent, **payload)
        if action == "remove_label":
            raise OSError("event bytes were written before failure")

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    board.update_task(task.id, remove_tags=["autopilot-staged"])
    assert "autopilot-staged" not in json.loads(path.read_text(encoding="utf-8"))["tags"]
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and "autopilot-staged" not in card.labels
    assert _recovery_records(tmp_path) == []


def test_cardstore_event_directory_symlink_is_rejected_at_open_time(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="hard0001", kind=Kind.TASK.value, title="safe path"))
    card_dir = tmp_path / "cards" / "hard0001"
    outside = tmp_path / "outside"
    outside.mkdir()
    (card_dir / "events").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="CardStore event directory"):
        store.append_event("hard0001", "claim", "owner", owner="owner")
    assert list(outside.iterdir()) == []


def test_card_event_log_symlink_is_rejected_at_open_time(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    card_events = tmp_path / "coordination" / "card_events"
    card_events.parent.mkdir()
    card_events.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="card event directory"):
        CardEventLog(tmp_path).append(CardEvent(card_id="hard0001", action="move"))
    assert list(outside.iterdir()) == []


def test_claim_and_complete_store_failures_restore_raw_projection_and_record_recovery(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    task = Task(id="hard0001", title="claim failure")
    board.create_task(task)
    original_mirror = board._mirror_card_store

    monkeypatch.setattr(
        board,
        "_mirror_card_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("claim mirror failed")),
    )
    with pytest.raises(OSError, match="claim mirror failed"):
        board.claim_task("owner", task.id)
    assert board.load_agent("owner") is None

    monkeypatch.setattr(board, "_mirror_card_store", original_mirror)
    board.claim_task("owner", task.id)
    monkeypatch.setattr(
        board,
        "_mirror_card_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("complete mirror failed")),
    )
    with pytest.raises(OSError, match="complete mirror failed"):
        board.complete_task("owner", task.id)
    agent = board.load_agent("owner")
    assert (
        agent is not None
        and task.id in agent.claimed_tasks
        and task.id not in agent.completed_tasks
    )
    records = _recovery_records(tmp_path)
    assert {record["operation"] for record in records} >= {"claim", "complete"}


def test_compensation_failure_is_fail_closed_and_durable(tmp_path, monkeypatch) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    monkeypatch.setattr(
        board,
        "_mirror_card_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("store failure")),
    )
    monkeypatch.setattr(
        board,
        "_restore_agent_projection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("restore failure")),
    )

    with pytest.raises(RuntimeError, match="fail-closed"):
        board.release_claim("owner", task.id, actor="repair")
    records = _recovery_records(tmp_path)
    assert any(record["phase"] == "legacy_restore_failed" for record in records)


@pytest.mark.parametrize("mode", ["1", "0", "dual"])
def test_staged_label_add_remove_parity_in_every_projection_mode(
    tmp_path, monkeypatch, mode
) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", mode)
    board = Board(tmp_path)
    task = Task(
        id="hard0001",
        title="staged",
        tags=["autopilot-staged", "initial"],
    )
    board.create_task(task)
    core_path = tmp_path / "cards" / task.id / "core.json"
    original_core = core_path.read_bytes() if core_path.exists() else None
    board.update_task(task.id, add_tags=["released"], remove_tags=["autopilot-staged"])

    legacy = next(view for view in board._legacy_task_views() if view.task.id == task.id)
    assert "autopilot-staged" not in legacy.task.tags
    assert "released" in legacy.task.tags
    assert task.id in board.unblocked_task_ids()
    if mode != "0":
        card = CardStore(tmp_path).fold(task.id)
        assert (
            card is not None
            and "autopilot-staged" not in card.labels
            and "released" in card.labels
        )
        actions = [event["action"] for event in CardStore(tmp_path)._read_events(task.id)]
        assert "remove_label" in actions and "add_label" in actions
        assert core_path.read_bytes() == original_core


def test_stale_release_uses_locked_preconditioned_card_store_transition(tmp_path) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    path = board.agent_projection_path("owner")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["last_seen"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert board.release_stale_claims("owner", older_than_seconds=1) == [task.id]
    event = next(
        item
        for item in CardStore(tmp_path)._read_events(task.id)
        if item.get("action") == "release_claim"
    )
    assert event["released_owner"] == "owner"
    assert event["expected_claim_revision"]


def test_joule_mint_can_reenter_board_only_after_completion_locks_release(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    complete = _create_claimed(board, "hard0001")
    secondary = Task(id="hard0002", title="secondary")
    board.create_task(secondary)
    observed: list[str] = []

    def reentrant_mint(active_board, task_id, agent_name) -> None:
        active_board.claim_task("nested", secondary.id)
        observed.append(task_id)

    monkeypatch.setattr(coordination_module, "_mint_joules_for_task", reentrant_mint)
    board.complete_task("owner", complete.id)

    assert observed == [complete.id]
    assert board.load_agent("nested").current_task == secondary.id


def test_durable_target_claim_and_preappend_demote_failure_restore_prior_state(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    previous = _create_claimed(board, "hard0001")
    target = Task(id="hard0002", title="target")
    board.create_task(target)
    projection = board.agent_projection_path("owner")
    original_projection = projection.read_bytes()
    original_mirror = board._mirror_card_store

    def fail_demote_before_append(op, **kwargs):
        if op == "demote":
            raise OSError("bumped demote failed before append")
        return original_mirror(op, **kwargs)

    monkeypatch.setattr(board, "_mirror_card_store", fail_demote_before_append)
    with pytest.raises(OSError, match="bumped demote failed before append"):
        board.claim_task("owner", target.id)

    assert projection.read_bytes() == original_projection
    agent = board.load_agent("owner")
    assert agent is not None and agent.current_task == previous.id
    previous_card = CardStore(tmp_path).fold(previous.id)
    target_card = CardStore(tmp_path).fold(target.id)
    assert previous_card is not None and previous_card.owner == "owner"
    assert previous_card.status == Column.DOING
    assert target_card is not None and target_card.owner is None
    assert target_card.status == Column.BACKLOG
    assert audit_lifecycle(tmp_path).clean is True


def test_durable_complete_after_error_mints_once_and_retry_mints_nothing(
    tmp_path, monkeypatch
) -> None:
    board = Board(tmp_path)
    task = _create_claimed(board)
    original_append = CardStore.append_event
    minted: list[str] = []

    def append_then_raise(self, card_id, action, agent, **payload):
        original_append(self, card_id, action, agent, **payload)
        if action == "complete":
            raise OSError("complete event wrote before error")

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    monkeypatch.setattr(
        coordination_module,
        "_mint_joules_for_task",
        lambda active_board, task_id, agent_name: minted.append(task_id),
    )

    board.complete_task("owner", task.id)
    assert minted == [task.id]
    monkeypatch.setattr(CardStore, "append_event", original_append)
    board.complete_task("owner", task.id)

    assert minted == [task.id]
    card = CardStore(tmp_path).fold(task.id)
    assert card is not None and card.status == Column.DONE and card.owner is None


def test_cardstore_read_paths_reject_symlinked_or_hardlinked_external_content(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="hard0001", kind=Kind.TASK.value, title="safe reads"))
    store.append_event("hard0001", "claim", "owner", owner="owner")
    card_dir = tmp_path / "cards" / "hard0001"
    outside = tmp_path / "outside"
    outside.mkdir()
    external_core = outside / "core.json"
    external_core.write_text('{"id":"external"}', encoding="utf-8")
    (card_dir / "core.json").unlink()
    (card_dir / "core.json").symlink_to(external_core)
    with pytest.raises(ValueError, match="unsafe"):
        store._load_core("hard0001")

    (card_dir / "core.json").unlink()
    os.link(external_core, card_dir / "core.json")
    with pytest.raises(ValueError, match="unsafe"):
        store._load_core("hard0001")

    (card_dir / "core.json").unlink()
    store.create(CardCore(id="hard0001", kind=Kind.TASK.value, title="safe reads"))
    writer = next((card_dir / "events").glob("*.jsonl"))
    external_events = outside / "events.jsonl"
    external_events.write_text('{"action":"complete"}\n', encoding="utf-8")
    writer.unlink()
    writer.symlink_to(external_events)
    with pytest.raises(ValueError, match="unsafe"):
        store._read_events("hard0001")


def test_cardstore_listing_and_card_event_reads_reject_symlinked_content(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="hard0001", kind=Kind.TASK.value, title="safe listing"))
    outside = tmp_path / "outside"
    outside.mkdir()
    card_dir = tmp_path / "cards" / "hard0001"
    card_dir.rename(outside / "hard0001")
    card_dir.symlink_to(outside / "hard0001", target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        store.list_card_ids()

    events = CardEventLog(tmp_path)
    events.append(CardEvent(card_id="hard0002", action="move"))
    event_file = next((tmp_path / "coordination" / "card_events").glob("*.jsonl"))
    external_event = outside / "card-events.jsonl"
    external_event.write_text('{"card_id":"external","action":"move"}\n', encoding="utf-8")
    event_file.unlink()
    event_file.symlink_to(external_event)
    with pytest.raises(ValueError, match="unsafe"):
        events.read_all()
