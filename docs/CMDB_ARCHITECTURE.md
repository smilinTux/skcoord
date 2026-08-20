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
- Impact traversal is bounded by depth and node count and reports cycles.
- Nor and chi operate separate canonical CMDBs. Reachability is not federation.

## Projection recovery

1. Stop the derived projection worker, not CMDB writers.
2. Delete or recreate only the derived index/schema.
3. Build one deterministic snapshot and record its SHA-256 checkpoint.
4. Replace the projection atomically from the snapshot.
5. Compare checkpoint and item count before resuming updates.

Projection failure leaves the canonical CMDB available and surfaces lag; it
never triggers reverse synchronization.
