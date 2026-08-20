"""One-way CMDB projection primitives.

Projection sinks receive immutable snapshots. They deliberately never receive
``CMDBManager``, so AGE/search adapters cannot become a second write path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

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


def build_snapshot(manager: CMDBManager) -> ProjectionSnapshot:
    """Build a deterministic, JSON-safe snapshot from the canonical fold."""
    items = tuple(
        ci.model_dump(mode="json") for ci in sorted(manager.list_cis(), key=lambda item: item.id)
    )
    payload = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return ProjectionSnapshot(
        checkpoint=hashlib.sha256(payload.encode()).hexdigest(), _payload=payload
    )


def project(manager: CMDBManager, sinks: Sequence[ProjectionSink]) -> dict[str, Any]:
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
    return {
        "checkpoint": snapshot.checkpoint,
        "items": len(snapshot.items),
        "completed_sinks": completed,
        "failed_sinks": failures,
        "complete": not failures,
        # Consumers must not persist/advertise a checkpoint until every sink
        # accepted the exact same snapshot.
        "committed_checkpoint": snapshot.checkpoint if not failures else None,
    }
