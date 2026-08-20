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
    items: tuple[dict[str, Any], ...]


class ProjectionSink(Protocol):
    """A replace-from-snapshot sink; no canonical-store handle is exposed."""

    def replace(self, snapshot: ProjectionSnapshot) -> None: ...


def build_snapshot(manager: CMDBManager) -> ProjectionSnapshot:
    """Build a deterministic, JSON-safe snapshot from the canonical fold."""
    items = tuple(
        ci.model_dump(mode="json") for ci in sorted(manager.list_cis(), key=lambda item: item.id)
    )
    encoded = json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    return ProjectionSnapshot(checkpoint=hashlib.sha256(encoded).hexdigest(), items=items)


def project(manager: CMDBManager, sinks: Sequence[ProjectionSink]) -> dict[str, Any]:
    """Replace every derived read model from the same canonical snapshot."""
    snapshot = build_snapshot(manager)
    completed = []
    for sink in sinks:
        sink.replace(snapshot)
        completed.append(type(sink).__name__)
    return {
        "checkpoint": snapshot.checkpoint,
        "items": len(snapshot.items),
        "completed_sinks": completed,
    }
