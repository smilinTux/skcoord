"""Audit and repair the mutable agent projection of kanban lifecycle state.

The event-sourced card store is authoritative for lifecycle and ownership. Agent
files remain a useful live projection, but a process can stop between those two
writes. This module detects that drift and provides an explicit, auditable repair
operation. It never deletes completion history for missing cards.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .atomic_io import atomic_write_text
from .card import CardEvent, CardEventLog, Column, KanbanBoard
from .card_store import card_store_write_enabled, mirror_coord_move
from .coordination import AgentFile, AgentState, Board


class LifecycleConflictError(RuntimeError):
    """Raised when automatic repair would overwrite genuinely active work."""


@dataclass(frozen=True)
class LifecycleIssue:
    """One disagreement between card state and its agent projection."""

    code: str
    task_id: str
    agent: str | None
    detail: str


@dataclass(frozen=True)
class LifecycleAudit:
    """Read-only reconciliation result."""

    card_count: int
    agent_count: int
    issues: tuple[LifecycleIssue, ...]

    @property
    def clean(self) -> bool:
        """Return whether every checked projection agrees."""
        return not self.issues

    def to_dict(self) -> dict:
        """Return a stable JSON-compatible representation."""
        return {
            "clean": self.clean,
            "card_count": self.card_count,
            "agent_count": self.agent_count,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class LifecycleRepairReceipt:
    """Receipt for one explicit projection repair."""

    actor: str
    repaired_at: str
    before: LifecycleAudit
    after: LifecycleAudit
    actions: tuple[str, ...]
    receipt_path: Path

    def to_dict(self) -> dict:
        """Return a stable JSON-compatible representation."""
        return {
            "actor": self.actor,
            "repaired_at": self.repaired_at,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "actions": list(self.actions),
        }


def _parse_seen(agent: AgentFile) -> datetime | None:
    """Parse an agent's last-seen timestamp as UTC."""
    try:
        seen = datetime.fromisoformat(agent.last_seen)
    except (TypeError, ValueError):
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen.astimezone(timezone.utc)


def _is_active(agent: AgentFile, stale_after_seconds: int) -> bool:
    """Return whether an agent is both declared and recently active."""
    if agent.state != AgentState.ACTIVE:
        return False
    seen = _parse_seen(agent)
    if seen is None:
        return False
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    return age <= stale_after_seconds


@contextmanager
def _lifecycle_lock(home: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize lifecycle transitions and repairs without restamping agents."""
    directory = Path(home) / "coordination"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".lifecycle.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _audit_lifecycle_unlocked(home: Path, *, task_ids: set[str] | None = None) -> LifecycleAudit:
    """Compare card state with agent projections while the caller holds a lock."""
    board = Board(home)
    cards = {
        card.id: card
        for card in KanbanBoard(home).cards(include_archived=True)
        if task_ids is None or card.id in task_ids
    }
    agents = {agent.agent: agent for agent in board.load_agents()}
    issues: list[LifecycleIssue] = []
    holders: dict[str, list[str]] = {}

    for agent in agents.values():
        if agent.current_task and agent.current_task not in agent.claimed_tasks:
            if task_ids is None or agent.current_task in task_ids:
                issues.append(
                    LifecycleIssue(
                        "current_not_claimed",
                        agent.current_task,
                        agent.agent,
                        "current_task is absent from claimed_tasks",
                    )
                )
        for task_id in agent.claimed_tasks:
            if task_ids is not None and task_id not in task_ids:
                continue
            holders.setdefault(task_id, []).append(agent.agent)
            card = cards.get(task_id)
            if card is None:
                issues.append(
                    LifecycleIssue("orphan_claim", task_id, agent.agent, "claimed card is missing")
                )
            elif card.status == Column.DONE:
                issues.append(
                    LifecycleIssue(
                        "done_still_claimed", task_id, agent.agent, "done card remains claimed"
                    )
                )
            elif card.status == Column.BACKLOG:
                issues.append(
                    LifecycleIssue(
                        "backlog_still_claimed",
                        task_id,
                        agent.agent,
                        "backlog card remains claimed",
                    )
                )
            elif card.owner and card.owner != agent.agent:
                issues.append(
                    LifecycleIssue(
                        "claimant_not_owner",
                        task_id,
                        agent.agent,
                        f"card owner is {card.owner}",
                    )
                )
        if agent.current_task:
            if task_ids is None or agent.current_task in task_ids:
                card = cards.get(agent.current_task)
                if card is not None and card.status == Column.REVIEW:
                    issues.append(
                        LifecycleIssue(
                            "review_reported_active",
                            agent.current_task,
                            agent.agent,
                            "review owner is still reported as active execution",
                        )
                    )
                elif card is not None and card.status == Column.DONE:
                    issues.append(
                        LifecycleIssue(
                            "done_reported_active",
                            agent.current_task,
                            agent.agent,
                            "done card is still reported as active execution",
                        )
                    )
        for task_id in agent.completed_tasks:
            if task_ids is not None and task_id not in task_ids:
                continue
            card = cards.get(task_id)
            if card is not None and card.status != Column.DONE:
                issues.append(
                    LifecycleIssue(
                        "reopened_still_completed",
                        task_id,
                        agent.agent,
                        "non-done card remains in completed_tasks",
                    )
                )

    for task_id, owners in holders.items():
        if len(set(owners)) > 1:
            issues.append(
                LifecycleIssue(
                    "multiple_claimants",
                    task_id,
                    None,
                    f"card is claimed by {', '.join(sorted(set(owners)))}",
                )
            )

    for card in cards.values():
        if not card.owner:
            continue
        owner = agents.get(card.owner)
        if card.status in (Column.READY, Column.REVIEW):
            if owner is None or card.id not in owner.claimed_tasks:
                issues.append(
                    LifecycleIssue(
                        "owner_claim_missing",
                        card.id,
                        card.owner,
                        f"{card.status.value} owner lacks a claimed projection",
                    )
                )
        elif card.status == Column.DOING:
            if owner is None or owner.current_task != card.id:
                issues.append(
                    LifecycleIssue(
                        "doing_owner_not_current",
                        card.id,
                        card.owner,
                        "doing owner does not report the card as current",
                    )
                )

    ordered = tuple(sorted(issues, key=lambda item: (item.task_id, item.code, item.agent or "")))
    return LifecycleAudit(len(cards), len(agents), ordered)


def audit_lifecycle(home: Path, *, task_ids: set[str] | None = None) -> LifecycleAudit:
    """Compare event-sourced card state with every agent's live projection."""
    with _lifecycle_lock(home, exclusive=False):
        return _audit_lifecycle_unlocked(home, task_ids=task_ids)


def _assert_no_active_conflicts(
    board: Board, audit: LifecycleAudit, stale_after_seconds: int
) -> None:
    """Reject repairs that could overwrite active ownership or active orphan work."""
    agents = {agent.agent: agent for agent in board.load_agents()}
    by_task: dict[str, set[str]] = {}
    for issue in audit.issues:
        if issue.code == "orphan_claim" and issue.agent:
            agent = agents[issue.agent]
            if _is_active(agent, stale_after_seconds):
                raise LifecycleConflictError(
                    f"active orphan claim {issue.task_id} held by {issue.agent}"
                )
        if issue.code == "claimant_not_owner" and issue.agent:
            agent = agents[issue.agent]
            if _is_active(agent, stale_after_seconds):
                raise LifecycleConflictError(
                    f"active non-owner claim {issue.task_id} held by {issue.agent}"
                )
        if issue.code == "multiple_claimants":
            for agent in agents.values():
                if issue.task_id in agent.claimed_tasks and _is_active(agent, stale_after_seconds):
                    by_task.setdefault(issue.task_id, set()).add(agent.agent)
    for task_id, owners in by_task.items():
        if len(owners) > 1:
            raise LifecycleConflictError(
                f"multiple active owners for {task_id}: {', '.join(sorted(owners))}"
            )


def _safe_actor(actor: str) -> str:
    """Return a filename-safe actor identifier."""
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", actor).strip("-.")
    return value or "operator"


def _append_receipt(home: Path, actor: str, payload: dict) -> Path:
    """Append one repair receipt to a per-writer conflict-free event log."""
    coordination = Path(home) / "coordination"
    directory = coordination / "reconciliation"
    if coordination.is_symlink() or directory.is_symlink():
        raise LifecycleConflictError("reconciliation receipt path contains a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir() or directory.resolve(strict=True).parent != coordination.resolve(
        strict=True
    ):
        raise LifecycleConflictError("reconciliation receipt path escapes coordination")
    path = directory / f"{_safe_actor(actor)}@{socket.gethostname()}.jsonl"
    flags = os.O_CREAT | os.O_APPEND | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LifecycleConflictError("reconciliation receipt destination is unsafe") from exc
    receipt_stat = os.fstat(descriptor)
    if not stat.S_ISREG(receipt_stat.st_mode) or receipt_stat.st_nlink != 1:
        os.close(descriptor)
        raise LifecycleConflictError("reconciliation receipt destination is unsafe")
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0, 2)
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return path


def _unresolved_receipt_ids(
    home: Path,
) -> tuple[tuple[str, frozenset[str] | None], ...]:
    """Return durable repair intents and the task scope each must reconcile."""
    directory = Path(home) / "coordination" / "reconciliation"
    if not directory.exists():
        return ()
    if directory.is_symlink() or not directory.is_dir():
        raise LifecycleConflictError("reconciliation receipt path is unsafe")
    phases: dict[str, set[str]] = {}
    scopes: dict[str, frozenset[str] | None] = {}
    for path in sorted(directory.glob("*.jsonl")):
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise LifecycleConflictError("reconciliation receipt source is unsafe") from exc
        receipt_stat = os.fstat(descriptor)
        if not stat.S_ISREG(receipt_stat.st_mode) or receipt_stat.st_nlink != 1:
            os.close(descriptor)
            raise LifecycleConflictError("reconciliation receipt source is unsafe")
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                lines = handle.read().splitlines()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        for line in lines:
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LifecycleConflictError("reconciliation receipt source is invalid") from exc
            if not isinstance(payload, dict):
                raise LifecycleConflictError("reconciliation receipt source is invalid")
            receipt_id = payload.get("receipt_id")
            phase = payload.get("phase")
            if receipt_id is None and phase is None:
                continue
            if not isinstance(receipt_id, str):
                raise LifecycleConflictError("reconciliation receipt source is invalid")
            if phase is None:
                phases.setdefault(receipt_id, set()).add("legacy_committed")
                continue
            if not isinstance(phase, str):
                raise LifecycleConflictError("reconciliation receipt source is invalid")
            phases.setdefault(receipt_id, set()).add(phase)
            if phase == "intent":
                raw_scope = payload.get("task_ids")
                if raw_scope is None:
                    scope = None
                elif isinstance(raw_scope, list) and all(
                    isinstance(task_id, str) for task_id in raw_scope
                ):
                    scope = frozenset(raw_scope)
                else:
                    raise LifecycleConflictError("reconciliation receipt scope is invalid")
                if receipt_id in scopes and scopes[receipt_id] != scope:
                    raise LifecycleConflictError("reconciliation receipt scope is inconsistent")
                scopes[receipt_id] = scope
    terminal = {"committed", "rolled_back", "recovered", "legacy_committed"}
    return tuple(
        sorted(
            (receipt_id, scopes.get(receipt_id))
            for receipt_id, seen in phases.items()
            if "intent" in seen and not seen & terminal
        )
    )


def _save_projection(board: Board, agent: AgentFile) -> Path:
    """Persist a repaired projection without forging agent liveness."""
    board.ensure_dirs()
    try:
        path = board.agent_projection_path(agent.agent)
    except ValueError as exc:
        raise LifecycleConflictError(str(exc)) from exc
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LifecycleConflictError("agent projection destination is unsafe")
    atomic_write_text(path, json.dumps(agent.model_dump(), indent=2) + "\n")
    return path


def _restore_projections(originals: dict[Path, bytes | None]) -> None:
    """Restore every projection touched by a failed repair attempt."""
    for path, original in reversed(tuple(originals.items())):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise LifecycleConflictError("cannot restore an unsafe agent projection")
        if original is None:
            path.unlink(missing_ok=True)
            continue
        atomic_write_text(path, original.decode("utf-8"))


def repair_lifecycle(
    home: Path,
    *,
    actor: str,
    stale_after_seconds: int = 3600,
    task_ids: set[str] | None = None,
) -> LifecycleRepairReceipt:
    """Repair agent projections to the card store and append a receipt.

    Missing-card completion history is deliberately preserved. A recent active
    orphan claim or multiple recent active claimants stops the entire repair.
    """
    with _lifecycle_lock(home, exclusive=True):
        return _repair_lifecycle_unlocked(
            home,
            actor=actor,
            stale_after_seconds=stale_after_seconds,
            task_ids=task_ids,
        )


def _repair_lifecycle_unlocked(
    home: Path,
    *,
    actor: str,
    stale_after_seconds: int,
    task_ids: set[str] | None,
) -> LifecycleRepairReceipt:
    """Repair projections while the caller holds the lifecycle lock."""
    board = Board(home)
    unresolved_receipts = _unresolved_receipt_ids(home)
    before = _audit_lifecycle_unlocked(home, task_ids=task_ids)
    _assert_no_active_conflicts(board, before, stale_after_seconds)
    cards = {
        card.id: card
        for card in KanbanBoard(home).cards(include_archived=True)
        if task_ids is None or card.id in task_ids
    }
    agents = {agent.agent: agent for agent in board.load_agents()}
    actions: list[str] = []
    originals: dict[Path, bytes | None] = {}
    pending: dict[str, AgentFile] = {}
    intent_written = False
    receipt_id = uuid.uuid4().hex
    repaired_at = datetime.now(timezone.utc).isoformat()

    def stage(agent: AgentFile) -> None:
        try:
            path = board.agent_projection_path(agent.agent)
        except ValueError as exc:
            raise LifecycleConflictError(str(exc)) from exc
        if path not in originals:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise LifecycleConflictError("agent projection destination is unsafe")
            originals[path] = path.read_bytes() if path.exists() else None
        pending[agent.agent] = AgentFile.model_validate(agent.model_dump())

    try:
        for agent in agents.values():
            original = agent.model_dump()
            retained_claims: list[str] = []
            for task_id in agent.claimed_tasks:
                if task_ids is not None and task_id not in task_ids:
                    retained_claims.append(task_id)
                    continue
                card = cards.get(task_id)
                if card is None:
                    if _is_active(agent, stale_after_seconds):
                        retained_claims.append(task_id)
                    else:
                        actions.append(f"release orphan {task_id} from {agent.agent}")
                elif card.status in (Column.BACKLOG, Column.DONE):
                    actions.append(f"release {card.status.value} {task_id} from {agent.agent}")
                elif card.owner and card.owner != agent.agent:
                    actions.append(f"release non-owner {task_id} from {agent.agent}")
                else:
                    retained_claims.append(task_id)
            agent.claimed_tasks = list(dict.fromkeys(retained_claims))
            current_task = agent.current_task
            current_in_scope = task_ids is None or current_task in task_ids
            if current_in_scope and current_task not in agent.claimed_tasks:
                if current_task:
                    actions.append(f"clear current {current_task} from {agent.agent}")
                agent.current_task = None
            elif (
                current_in_scope
                and current_task is not None
                and cards.get(current_task) is not None
                and cards[current_task].status != Column.DOING
            ):
                actions.append(f"stop active {current_task} for {agent.agent}")
                agent.current_task = None
            reopened = [
                task_id
                for task_id in agent.completed_tasks
                if task_id in cards
                and cards[task_id].status != Column.DONE
                and (task_ids is None or task_id in task_ids)
            ]
            if reopened:
                agent.completed_tasks = [
                    task_id for task_id in agent.completed_tasks if task_id not in reopened
                ]
                actions.extend(f"reopen {task_id} for {agent.agent}" for task_id in reopened)
            if agent.model_dump() != original:
                stage(agent)

        for card in cards.values():
            if not card.owner:
                continue
            owner = agents.get(card.owner) or AgentFile(agent=card.owner, state=AgentState.OFFLINE)
            original = owner.model_dump()
            if card.status == Column.DONE:
                if card.id not in owner.completed_tasks:
                    owner.completed_tasks.append(card.id)
                    actions.append(f"complete {card.id} for {owner.agent}")
            elif card.status in (Column.READY, Column.REVIEW, Column.DOING):
                if card.id not in owner.claimed_tasks:
                    owner.claimed_tasks.append(card.id)
                    actions.append(f"claim {card.id} for {owner.agent}")
                if card.id in owner.completed_tasks:
                    owner.completed_tasks.remove(card.id)
                if card.status == Column.DOING:
                    owner.current_task = card.id
                elif owner.current_task == card.id:
                    owner.current_task = None
            if owner.model_dump() != original:
                stage(owner)
                agents[owner.agent] = owner

        intent = {
            "receipt_id": receipt_id,
            "phase": "intent",
            "actor": actor,
            "repaired_at": repaired_at,
            "before": before.to_dict(),
            "actions": actions,
            "projection_agents": sorted(pending),
            "task_ids": sorted(task_ids) if task_ids is not None else None,
        }
        _append_receipt(home, actor, intent)
        intent_written = True
        for agent in pending.values():
            _save_projection(board, agent)
        after = _audit_lifecycle_unlocked(home, task_ids=task_ids)
        if not after.clean:
            raise LifecycleConflictError("repair did not converge; rerun audit for details")
        payload = {
            "receipt_id": receipt_id,
            "phase": "committed",
            "actor": actor,
            "repaired_at": repaired_at,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "actions": actions,
        }
        receipt_path = _append_receipt(home, actor, payload)
        current_scope = frozenset(task_ids) if task_ids is not None else None
        recoverable = (
            (unresolved_id, unresolved_scope)
            for unresolved_id, unresolved_scope in unresolved_receipts
            if current_scope is None
            or (unresolved_scope is not None and unresolved_scope <= current_scope)
        )
        for unresolved_id, unresolved_scope in recoverable:
            _append_receipt(
                home,
                actor,
                {
                    "receipt_id": unresolved_id,
                    "phase": "recovered",
                    "actor": actor,
                    "repaired_at": repaired_at,
                    "recovered_by": receipt_id,
                    "task_ids": (
                        sorted(unresolved_scope) if unresolved_scope is not None else None
                    ),
                    "after": after.to_dict(),
                },
            )
    except Exception:
        try:
            _restore_projections(originals)
        except Exception as rollback_exc:
            raise LifecycleConflictError(
                "repair failed and projection rollback failed"
            ) from rollback_exc
        if intent_written:
            try:
                _append_receipt(
                    home,
                    actor,
                    {
                        "receipt_id": receipt_id,
                        "phase": "rolled_back",
                        "actor": actor,
                        "repaired_at": repaired_at,
                        "before": before.to_dict(),
                        "actions": actions,
                    },
                )
            except Exception:
                pass
        raise
    return LifecycleRepairReceipt(
        actor=actor,
        repaired_at=repaired_at,
        before=before,
        after=after,
        actions=tuple(actions),
        receipt_path=receipt_path,
    )


def transition_task(
    home: Path,
    *,
    task_id: str,
    column: Column | str,
    actor: str,
    order: int | None = None,
) -> LifecycleRepairReceipt:
    """Move one card and reconcile its agent projection before returning.

    Active projection conflicts are rejected before the move. If a later write
    fails, compensating move events restore the prior folded card state before
    the error is returned.
    """
    target = Column(column)
    root = Path(home)
    with _lifecycle_lock(root, exclusive=True):
        cards = {card.id: card for card in KanbanBoard(root).cards(include_archived=True)}
        current = cards.get(task_id)
        if current is None:
            raise ValueError(f"Task {task_id} not found")
        before = _audit_lifecycle_unlocked(root, task_ids={task_id})
        _assert_no_active_conflicts(Board(root), before, stale_after_seconds=3600)

        def append_move(column_value: str, position: int | None) -> None:
            CardEventLog(root).append(
                CardEvent(
                    card_id=task_id,
                    action="move",
                    column=column_value,
                    order=position,
                    writer=actor,
                )
            )
            if card_store_write_enabled():
                mirror_coord_move(root, task_id, column_value, actor, order=position)

        try:
            append_move(target.value, order)
            return _repair_lifecycle_unlocked(
                root,
                actor=actor,
                stale_after_seconds=3600,
                task_ids={task_id},
            )
        except Exception:
            try:
                append_move(current.status.value, current.order)
                _repair_lifecycle_unlocked(
                    root,
                    actor=actor,
                    stale_after_seconds=3600,
                    task_ids={task_id},
                )
            except Exception as rollback_exc:
                raise LifecycleConflictError(
                    f"move failed and compensation did not converge for {task_id}"
                ) from rollback_exc
            raise
