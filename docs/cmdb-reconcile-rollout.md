# CMDB whole-network reconcile rollout

The orchestrator in `skcoord.cmdb_reconcile` is a library boundary. Installing
the package does not alter a timer, target set, or live CMDB.

## Verified rollout state (2026-08-20)

Three distinct checksum-valid, complete credentialed shadows passed with the
same reviewed scope fingerprint
`3cb1e77f416bc14fd949f24502bf11887d046ab2b26150856843b838ea11fe7e`.
Targets are `noroc2027`, `192.168.0.41`, and `192.168.0.100`; each target has
an exact `skvault://` SSH reference and no inline secret. Change
`chg-a543c87b` has a single-use authenticated Chef approval carrying role,
fingerprint, and authorization provenance. Signing the protected/freeze plane
did not lift the fleet freeze.

The remaining cutover is operational, not a waiver: install the tagged
`skcapstone-cmdb-reconcile-network.service` and its owner-reviewed `0700`
launcher, complete the fault/rollback drill, make generic ATLAS report-only
during the narrow change window, then run the governed action. The old
`skcapstone-cmdb-reconcile.timer` still targets local-only apply and remains
the rollback path until network apply and artifact/audit readback succeed.

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

Prepare an explicit ownership manifest (never derive it from scan output), then
run the machine gate:

```bash
python -m skcoord.cmdb_rollout --home /scratch/skcapstone \
  --manifest ownership.json \
  --artifact run-1.json --artifact run-2.json --artifact run-3.json
```

Every artifact needs its adjacent `.sha256`. The command rejects applied or
partial runs, duplicate scan IDs, checksum failures, and target/collector scope
changes. Its preview prints a `plan_digest`; review and record that exact JSON.

```json
{
  "schema": "skcoord.cmdb.ownership-backfill/v1",
  "entries": [{
    "ci_id": "ci-service-example",
    "authority": "systemd:nor",
    "scope_fingerprint": "<64 lowercase hex characters>"
  }]
}
```

Apply only to a backed-up store by repeating the command with `--apply` and
`--approval-digest <plan_digest>`. Enrollment appends authoritative
`discovered`, `source_authority`, and `lifecycle_scope` events and writes a
checksummed record under `cmdb/ownership-backfills/`. It refuses retired,
missing, already-owned, or post-review-changed CIs.

## Cutover

Pin the reviewed target/config and code versions in the timer invocation. Keep
the existing unit installed but inactive. The first scheduled run remains a
dry-run; enable `apply` only after its artifact passes the same checks as the
shadow runs. Alert when no complete artifact arrives inside the authority's
freshness SLO.

### Timer cutover checklist

1. Capture `systemctl --user cat`, `show`, and `list-timers` output for both
   units. Record effective command, code version, environment, calendar, and
   last result. Reading these is not approval to mutate them.
2. Install the candidate shadow oneshot and the distinct
   `skcapstone-cmdb-reconcile-network.service`; shadow must omit `--apply`, pin
   reviewed targets, and retain artifacts. Network apply must be reachable only
   through the guarded launcher and ATLAS change gate.
3. Pass the three-run machine gate and ownership preview. Back up the complete
   CMDB before enrollment.
4. Require an approved change record naming both units, artifact checksums,
   plan digest, rollback owner, and observation window.
5. Keep the fleet frozen through drills. Before the approved apply, remove
   generic `--honor` from the effective ATLAS schedule and verify report-only
   readback; only then may the human lift freeze for this narrow workflow.
6. Start network apply through
   `skcapstone cmdb operator act apply-cmdb-reconcile --change-id <id>`.
   Verify the resulting artifact checksum, completeness, scope, relationship
   audit, lifecycle outcome, and effective unit.
7. Stop/disable the old timer only after acceptance; keep its files installed.
   On any failed check, refreeze immediately and follow Rollback.

Missing credentials, reachability, backup evidence, checksums, approval, or
rollback authority is a blocker, not a reason to weaken a gate.

## Rollback

Disable the candidate timer and re-enable the previous timer. Do not delete run
artifacts or reverse additive CI events. If the candidate wrote an incorrect
status, restore the correct status with a new event and link the bad run's
artifact in the note. Manual retirement is never automatically reversed.
