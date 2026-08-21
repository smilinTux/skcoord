# CMDB operator runbook

## Safe verification

1. Run discovery without `--apply` and retain the run summary.
2. Confirm scan scope and completeness before interpreting missing CIs.
3. Audit relationships and inspect dangling targets before retirement.
4. Apply reconciliation only after the dry-run artifact is reviewed.
5. Retire rather than delete; preview affected dependents first.

Supported operator sequence:

```bash
skcapstone cmdb plan --network --record-run \
  --credential HOST=skvault://REFERENCE
skcapstone cmdb status --json
skcapstone cmdb apply --network \
  --credential HOST=skvault://REFERENCE
skcapstone cmdb drift --json
```

`cmdb reconcile` remains a timer-compatible alias. New automation should use
the explicit plan/apply verbs so write intent is visible in logs and reviews.
Never use the deprecated seed bridge for a fleet baseline.

## Evidence gate

A timer or fleet object is not proof. Record the effective command, exit code,
scan identifier, target completeness, created/updated/unchanged/orphan counts,
drift summary, and artifact checksum. Accept a release only when the fixture
suite and a live-safe dry run both pass.

Legacy CIs are not implicitly discovery-owned. Use the explicit manifest and
three-shadow-run gate in [cmdb-reconcile-rollout.md](cmdb-reconcile-rollout.md).
Shell access or an enabled timer does not satisfy the change, backup, scope, or
plan-digest gates.

## Backup and recovery

Back up the complete `cmdb/` directory so write-once cores and every writer's
event files stay together. Restore into a scratch home, fold all CIs, run the
relationship audit, and compare the deterministic projection checkpoint before
placing the store back in service.

## Physical schema migration (v1 to v2)

Use `CMDBManager(home).migrate_schema()` to inspect the complete store. The
default is a strict, write-free dry run. It reports v1 cores and events and
fails closed if any record is malformed, symlinked, or newer than the reader.

After stopping CMDB writers and reviewing the plan, call
`migrate_schema(apply=True)`. The API copies the complete `cmdb/` tree to a
same-filesystem staging directory, upgrades and folds the staged copy, then
uses atomic directory renames for cutover. The former tree is retained beside
the live tree as `cmdb.backup-<UTC timestamp>` and is the rollback artifact.
If cutover fails, the original path is restored. A store already at v2 is a
no-op and creates no additional backup.

Never point migration tests at the live home. Exercise both dry-run and apply
against a restored scratch copy first. To roll back, stop writers, move the v2
`cmdb/` aside, and atomically rename the retained backup to `cmdb/`.
