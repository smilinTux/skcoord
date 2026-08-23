# Sovereign CMDB architecture

`skcoord.cmdb.CMDBManager` is the single write authority. Its write-once CI
cores and append-only per-writer events are canonical. Dashboards, AGE graphs,
search indexes, reports, and network maps are disposable read models.

## Invariants

- A projection consumes `ProjectionSnapshot`; it never receives a manager or
  writes events back into the CMDB.
- CI retirement is a status event. Records and relationships are not deleted.
- Discovery distinguishes declarations from observations. An incomplete scan
  cannot prove that an asset is absent.
- Relationship repair begins with the read-only integrity audit.
- `alias_of` is a first-class host/device identity edge; retaining an old CI
  during canonicalisation must not make the integrity audit fail.
- Impact traversal is bounded by depth and node count and reports cycles.
- Nor and chi operate separate canonical CMDBs. Reachability is not federation.
- Evidence freshness is derived from `observed_at` when read. A stored
  `observation_state` is never authoritative because time passes without an event.
- Reconcile persists a collector's `lifecycle_scope` fingerprint with its
  observation. Orchestrators must stamp that field from the exact target and
  collector scope; a changed scope is new evidence, not a comparable miss pass.
- Discovery-owned tags and relationships carry their authority in events and
  converge within that authority. Core/manual tags and unowned relationships
  are preserved. Host/device alias resolution is shared by reconcile and drift;
  promotion creates a host CI and retains the old device as `alias_of` history.
- Readers reject future core or event schema versions. A v1-to-v2 migration starts
  from `migration_preview()`, a detached copy that leaves the canonical record intact.

## Projection recovery

1. Stop the derived projection worker, not CMDB writers.
2. Delete or recreate only the derived index/schema.
3. Build one deterministic snapshot and record its SHA-256 checkpoint.
4. Replace the projection atomically from the snapshot.
5. Compare checkpoint and item count before resuming updates.

Each sink receives an isolated snapshot view. A checkpoint is committed only
when every configured sink accepts that snapshot; partial projection results
surface failed sinks and leave the committed checkpoint unset.

`JsonProjectionSink` is the reference durable search/read-model sink. It
atomically replaces a versioned JSON document containing the snapshot checksum,
count, and CI records. `JsonCheckpointStore` atomically records the last
all-sinks-successful checksum separately from every sink. A failed sink or
checkpoint write leaves the previous checkpoint intact, making projection lag
observable across process restarts.

AGE integration is dependency-injected through `AgeProjectionAdapter` and
wrapped by `AgeProjectionSink`. The adapter operation is deliberately narrow:
`replace_projection(checkpoint, items)`. An AGE implementation should replace
its derived graph in one database transaction and store the supplied checksum
in that graph schema. It receives detached JSON-safe records, never a
`CMDBManager` or canonical filesystem path. Configure it only with a database
role restricted to the disposable projection graph.

```python
from pathlib import Path
from skcoord.cmdb_projection import (
    AgeProjectionSink, JsonCheckpointStore, JsonProjectionSink, project,
)

result = project(
    manager,
    [JsonProjectionSink(Path("var/cmdb-search.json")), AgeProjectionSink(age_adapter)],
    JsonCheckpointStore(Path("var/cmdb-projection-checkpoint.json")),
)
if not result["complete"]:
    raise RuntimeError(f"projection lag: {result['failed_sinks']}")
```

Projection failure leaves the canonical CMDB available and surfaces lag; it
never triggers reverse synchronization.
