# CMDB operator runbook

## Safe verification

1. Run discovery without `--apply` and retain the run summary.
2. Confirm scan scope and completeness before interpreting missing CIs.
3. Audit relationships and inspect dangling targets before retirement.
4. Apply reconciliation only after the dry-run artifact is reviewed.
5. Retire rather than delete; preview affected dependents first.

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
