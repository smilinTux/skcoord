"""One-way CMDB projection primitives.

Projection sinks receive immutable snapshots. They deliberately never receive
``CMDBManager``, so AGE/search adapters cannot become a second write path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .atomic_io import atomic_write_text
from .cmdb import CMDBManager


@dataclass(frozen=True)
class ProjectionSnapshot:
    checkpoint: str
    _payload: str

    @property
    def items(self) -> tuple[dict[str, Any], ...]:
        """Return a detached copy so one sink cannot mutate another's input."""
        return tuple(json.loads(self._payload))


class ProjectionSink(Protocol):
    """A replace-from-snapshot sink; no canonical-store handle is exposed."""

    def replace(self, snapshot: ProjectionSnapshot) -> None: ...


class ProjectionCheckpointStore(Protocol):
    """Durable record of the last snapshot accepted by the complete pipeline."""

    def commit(self, checkpoint: str, item_count: int, sinks: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class JsonProjectionSink:
    """Atomic, disposable JSON search/read-model projection.

    The file is a complete replacement index, rather than an event log.  It is
    safe to delete and rebuild from the canonical CMDB.
    """

    path: Path

    def replace(self, snapshot: ProjectionSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "checkpoint": snapshot.checkpoint,
            "item_count": len(snapshot.items),
            "items": snapshot.items,
        }
        atomic_write_text(
            self.path,
            json.dumps(document, indent=2, sort_keys=True, separators=(",", ": ")) + "\n",
        )


@dataclass(frozen=True)
class JsonCheckpointStore:
    """Atomic on-disk pipeline checkpoint, committed only after all sinks."""

    path: Path

    def commit(self, checkpoint: str, item_count: int, sinks: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "schema_version": 1,
            "checkpoint": checkpoint,
            "item_count": item_count,
            "sinks": list(sinks),
        }
        atomic_write_text(self.path, json.dumps(document, indent=2, sort_keys=True) + "\n")

    def load(self) -> dict[str, Any] | None:
        """Return the committed checkpoint, or ``None`` before the first run."""
        if not self.path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported projection checkpoint document")
        return value


class AgeProjectionAdapter(Protocol):
    """Injected AGE implementation boundary for a *derived* graph only.

    Implementations may transact against PostgreSQL/AGE, but receive neither a
    ``CMDBManager`` nor canonical paths and therefore cannot write CMDB events.
    """

    def replace_projection(
        self, checkpoint: str, items: tuple[dict[str, Any], ...]
    ) -> None: ...


@dataclass(frozen=True)
class AgeProjectionSink:
    """Operate an injected AGE adapter from an isolated snapshot."""

    adapter: AgeProjectionAdapter

    def replace(self, snapshot: ProjectionSnapshot) -> None:
        self.adapter.replace_projection(snapshot.checkpoint, snapshot.items)


def build_snapshot(manager: CMDBManager) -> ProjectionSnapshot:
    """Build a deterministic, JSON-safe snapshot from the canonical fold."""
    items = tuple(
        ci.model_dump(mode="json") for ci in sorted(manager.list_cis(), key=lambda item: item.id)
    )
    payload = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return ProjectionSnapshot(
        checkpoint=hashlib.sha256(payload.encode()).hexdigest(), _payload=payload
    )


def project(
    manager: CMDBManager,
    sinks: Sequence[ProjectionSink],
    checkpoint_store: ProjectionCheckpointStore | None = None,
) -> dict[str, Any]:
    """Replace every derived read model from the same canonical snapshot."""
    snapshot = build_snapshot(manager)
    completed = []
    failures: list[dict[str, str]] = []
    for sink in sinks:
        name = type(sink).__name__
        try:
            # A separate frozen envelope plus copy-on-access items prevents a
            # sink retaining or modifying state visible to any other sink.
            sink.replace(ProjectionSnapshot(snapshot.checkpoint, snapshot._payload))
        except Exception as exc:  # a projection failure must surface as lag
            failures.append({"sink": name, "error": type(exc).__name__})
        else:
            completed.append(name)
    committed_checkpoint = None
    if not failures:
        if checkpoint_store is None:
            committed_checkpoint = snapshot.checkpoint
        else:
            try:
                checkpoint_store.commit(snapshot.checkpoint, len(snapshot.items), completed)
            except Exception as exc:
                failures.append(
                    {"sink": type(checkpoint_store).__name__, "error": type(exc).__name__}
                )
            else:
                committed_checkpoint = snapshot.checkpoint
    return {
        "checkpoint": snapshot.checkpoint,
        "items": len(snapshot.items),
        "completed_sinks": completed,
        "failed_sinks": failures,
        "complete": not failures,
        # Consumers must not persist/advertise a checkpoint until every sink
        # accepted the exact same snapshot.
        "committed_checkpoint": committed_checkpoint,
    }
