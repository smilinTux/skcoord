# CMDB whole-network reconcile rollout

The orchestrator in `skcoord.cmdb_reconcile` is a library boundary. Installing
the package does not alter a timer, target set, or live CMDB.

## Shadow gate

1. Resolve targets from fleet node objects plus an explicitly reviewed source
   map. Save the emitted host/provenance pairs with the candidate config.
2. Run at least three candidate scans with `apply=False` alongside the existing
   timer. Retain each JSON artifact and SHA-256 sidecar.
3. Reject the cutover if any candidate is incomplete, exceeds its deadline or
   failure budget, omits an expected target, or emits absence drift that the
   existing scan cannot corroborate.
4. Compare created, updated, unchanged, orphan, drift, and per-target collector
   counts. Explain every difference; do not use a numeric tolerance to excuse a
   missing host.
5. Preview lifecycle actions separately. A partial scan must produce no miss
   increments or retirement candidates.

## Cutover

Pin the reviewed target/config and code versions in the timer invocation. Keep
the existing unit installed but inactive. The first scheduled run remains a
dry-run; enable `apply` only after its artifact passes the same checks as the
shadow runs. Alert when no complete artifact arrives inside the authority's
freshness SLO.

## Rollback

Disable the candidate timer and re-enable the previous timer. Do not delete run
artifacts or reverse additive CI events. If the candidate wrote an incorrect
status, restore the correct status with a new event and link the bad run's
artifact in the note. Manual retirement is never automatically reversed.
