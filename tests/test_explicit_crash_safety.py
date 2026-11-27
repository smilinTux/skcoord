"""Fault-injection coverage for explicit-ID creation crash safety.

PR122 review REQUEST_CHANGES on 5c5f2251:
1. A ``create_explicit`` write/fsync failure must not leave an ``accepted``
   attempt with a missing or partial ``core.json``.
2. Accepted-record recovery must revalidate the stored core against the
   request digest instead of trusting the caller-supplied digest.
3. Board replay of an existing legacy task file must be content-compared and
   fail closed on symlink/non-regular/multi-link files.
4. ``create_claimed_task`` must recognize an exact replay before checking
   current mutable dependencies.
"""

import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from skcoord.card_store import (
    CardCore,
    CardStore,
    explicit_creation_request_digest,
)
from skcoord.coordination import Board, Task


def _core(card_id: str, title: str) -> CardCore:
    return CardCore(id=card_id, title=title)


def _task(task_id: str, title: str) -> Task:
    return Task(id=task_id, title=title)


def _digest(task: Task) -> str:
    return explicit_creation_request_digest(task.model_dump(mode="json"))


def test_write_failure_does_not_leave_accepted_ledger(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    digest = "e" * 64

    class FailingDirectory:
        def __init__(self, *args, **kwargs):
            self.mode = "r"

        def close(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with mock.patch("skcoord.card_store.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises((ValueError, OSError)):
            store.create_explicit(_core("crash01", "Crash"), digest, "maker")

    assert store.fold("crash01") is None
    attempts = [
        record
        for record in store._read_creation_attempts()
        if record.get("card_id") == "crash01"
    ]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "rejected"

    digest2 = "f" * 64
    assert store.create_explicit(_core("crash01", "Crash"), digest2, "maker") is True
    assert store.fold("crash01") is not None


def test_write_failure_leaves_no_partial_core(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    digest = "9" * 64

    real_open = os.open
    opened: list[int] = []
    core_fd: list[int] = []

    def tracking_open(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        name_s = name if isinstance(name, (str, bytes)) else str(name)
        if isinstance(name_s, bytes):
            name_s = name_s.decode()
        if (flags & os.O_CREAT) and (flags & os.O_EXCL) and "core.json" in name_s:
            core_fd.append(fd)
        opened.append(fd)
        return fd

    with mock.patch("skcoord.card_store.os.open", side_effect=tracking_open), mock.patch(
        "skcoord.card_store.os.fsync",
        side_effect=lambda fd: (_ for _ in ()).throw(OSError("ENOSPC"))
        if fd in core_fd else None,
    ):
        with pytest.raises((ValueError, OSError)):
            store.create_explicit(_core("crash02", "Crash"), digest, "maker")

    card_dir = tmp_path / "cards" / "crash02"
    assert not (card_dir / "core.json").is_file()


def test_accepted_recovery_recomputes_digest_instead_of_trusting_caller(
    tmp_path: Path,
) -> None:
    store = CardStore(tmp_path)
    core = _core("replay01", "Replay")
    assert store.create_explicit(core, _digest_from_core(core), "maker")

    matching = _digest_from_core(core)
    assert store.create_explicit(core, matching, "maker") is False

    with pytest.raises(ValueError, match="explicit creation conflict"):
        store.create_explicit(core, "f" * 64, "maker")

    divergent = CardCore(id="replay01", title="Divergent")
    with pytest.raises(ValueError, match="explicit creation conflict"):
        store.create_explicit(divergent, _digest_from_core(divergent), "maker")


def _digest_from_core(core: CardCore) -> str:
    payload = json.dumps(core.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_board_replay_existing_legacy_is_content_bound(tmp_path: Path) -> None:
    board = Board(tmp_path)
    task = _task("legacy01", "Legacy")
    path = board.create_explicit_task(task, _digest(task), "maker")

    before = path.read_bytes()
    assert board.create_explicit_task(task, _digest(task), "maker") == path
    assert path.read_bytes() == before

    other = Task(id="legacy01", title="Other")
    with pytest.raises(ValueError, match="explicit creation conflict"):
        board.create_explicit_task(other, _digest(other), "maker")
    assert path.read_bytes() == before


def test_board_replay_existing_legacy_rejects_unsafe_file(tmp_path: Path) -> None:
    board = Board(tmp_path)
    task = _task("legacy02", "Legacy")
    path = board.create_explicit_task(task, _digest(task), "maker")
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "legacy02", "title": "Impostor"}\n')
    path.unlink()
    os.symlink(outside, path)

    with pytest.raises(ValueError, match="unsafe"):
        board.create_explicit_task(task, _digest(task), "maker")
    assert not path.exists()
    # Durable card remains; only the unsafe legacy projection was refused.
    assert [view.task.id for view in board.get_task_views()] == ["legacy02"]


def test_claimed_explicit_replay_wins_before_dependency_check(tmp_path: Path) -> None:
    board = Board(tmp_path)
    dep = Task(id="open1", title="Dependency")
    board.create_task(dep)
    board.claim_task("maker", dep.id)
    board.complete_task("maker", dep.id)

    task = Task(id="claim01", title="Claimed", dependencies=["open1"])
    digest = _digest(task)
    path, revision = board.create_claimed_task(task, "maker", digest, "maker")
    assert (tmp_path / "cards" / "claim01" / "core.json").is_file()

    # Reason: drop the dependency so a mutable re-check would fail closed.
    for leftover in board.tasks_dir.glob("open1-*.json"):
        leftover.unlink()
    shutil.rmtree(tmp_path / "cards" / "open1", ignore_errors=True)

    replay_path, replay_revision = board.create_claimed_task(
        task, "maker", digest, "maker"
    )
    assert replay_path == path
    assert replay_revision == revision


def test_claimed_new_create_still_validates_dependencies(tmp_path: Path) -> None:
    board = Board(tmp_path)
    open_task = Task(id="open2", title="Dependency")
    board.create_task(open_task)
    task = Task(id="claim02", title="Claimed", dependencies=["open2"])

    with pytest.raises(ValueError, match="incomplete dependencies"):
        board.create_claimed_task(task, "maker", _digest(task), "maker")
    assert not list(board.tasks_dir.glob("claim02-*.json"))
