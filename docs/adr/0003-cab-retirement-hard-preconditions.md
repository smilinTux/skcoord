# ADR 0003: CAB retirement and hard preconditions for change execution

Status: accepted

Date: 2026-08-29

## Context

The Change Advisory Board (CAB) was designed to provide a second pair of eyes on destructive changes. In a five-node private estate with one operator and one agent, a CAB becomes a single agent voting on its own change. The vote cannot fail, which teaches everyone that gates are noise.

Change chg-ca4d0ea5 (2026-08-29) demonstrated this issue: it carried a CAB decision record that was purely ceremonial. What actually caught the problem was not the vote, but three other controls:
1. A fail-closed preflight that stopped before any mutation on a genuine custody mismatch
2. A rollback plan that required verified custody supplement before removal
3. Chef explicit approval

The vote added nothing to the safety of the change. A gate that cannot fail is worse than no gate because it teaches that gates are noise.

## Decision

Retire the CAB voting requirement as a gate for change execution. Replace it with three hard preconditions that must be satisfied before any destructive or high-risk change can proceed:

1. **Fail-closed preflight**: A preflight check that runs before any mutation and stops execution if it fails, with a clear explanation of why.
2. **Stated rollback plan**: Destructive and high-risk changes must have a rollback plan documented in their core.json.
3. **Operator explicit approval**: The operator must explicitly approve the change (this remains unchanged from the existing model).

These controls already exist as conventions. This ADR makes them hard conditions that block execution when absent.

### CAB retirement implementation

- CAB votes are no longer read or applied in the fold unless `SKCOORD_ITIL_CAB_ENABLED=1` is set
- The `cab_required` field in Change core.json is retained for historical record but no longer gates execution
- Existing CAB vote records in `cab-decisions/` are preserved as history, not deleted
- A new `itil_config.py` module provides a single setting to re-enable CAB if needed later

### Hard preconditions enforcement

- Destructive changes (EMERGENCY type or HIGH risk) must have a non-empty `rollback_plan` in core.json
- Destructive changes must have a `preflight_passed` event (no outstanding `preflight_failed` events)
- When a change attempts a transition to `implementing` or `deployed`, the fold checks these conditions and blocks the transition with a conflict entry if they are not met
- The conflict entry clearly states which condition failed and why

## Re-enabling CAB

CAB can be re-enabled with a single environment variable:

```bash
export SKCOORD_ITIL_CAB_ENABLED=1
```

Or via a configuration file at `~/.skcapstone/config/itil-cab.yaml`:

```yaml
cab_enabled: true
notes: "Re-enabled for audit trail requirement"
```

When CAB is re-enabled:
- CAB votes are read and folded into change status (original behavior)
- The hard preconditions (preflight, rollback plan, operator approval) still apply
- CAB adds a second pair of eyes and formal approval record, but does not replace the hard checks

### What CAB adds beyond preflight when enabled

1. **Formal approval record**: Votes with conditions from multiple agents, persisted in cab-decisions/
2. **Historical audit trail**: Who approved what and why, with timestamps and conditions
3. **Multi-agent coordination**: Useful if the estate grows beyond one operator
4. **Explicit rejection path**: A rejection vote blocks without needing operator intervention

Tradeoff: In the current estate, CAB is ceremonial. The hard preconditions are the real safety net.

## Consequences

### Positive

- Gates that can fail: preflight and rollback plan checks are real, enforceable conditions
- Clear failure reasons: when a change is blocked, the conflict entry says exactly why
- Simpler workflow: no waiting for a vote that cannot fail
- Preserved history: existing CAB decisions remain in the record
- Reversible: CAB can be re-enabled with one setting if the estate grows

### Negative

- Lost formal approval record: when CAB is disabled, there is no separate vote record
- Operator approval is now the only human gate (which was already true in practice)

### Neutral

- The `cab_required` field is retained in core.json for historical compatibility
- Existing changes are not rewritten: their `cab_required` value stays as recorded
- The retirement is recorded in the timeline of active changes, not erased from history

## Implementation details

### New event kinds

- `preflight_passed`: Records that preflight checks succeeded (optional event)
- `preflight_failed`: Records that preflight checks failed with a reason (blocks execution)

### New Change fields

- `preflight_status`: Derived from events, one of `"passed"`, `"failed"`, or `None`
- `preflight_reason`: Reason why preflight failed, if it did

### New ITILManager methods

- `record_preflight_passed(change_id, agent, note)`: Record a successful preflight
- `record_preflight_failed(change_id, agent, reason)`: Record a failed preflight
- `check_execution_preconditions(change_id)`: Read-only check of hard preconditions

### Configuration

New module `itil_config.py` provides:
- `cab_enabled()`: Check if CAB voting is enabled
- `rollback_plan_required_for_risk(risk)`: Check if rollback is required for a risk level
- `rollback_plan_required_for_change_type(change_type)`: Check if rollback is required for a change type
- `is_destructive_change(change_type, risk)`: Check if a change is destructive/high-risk
- `preflight_required_for_change(change_type, risk)`: Check if preflight is required

## References

- Card 4655a851: [ITIL-CAB-RETIRE-01][M] Retire cab_required across ITIL
- Change chg-ca4d0ea5: Contain and retire the legacy ~/.capauth private-key synchronization surface
- ADR 0002: Legacy CAB votes and the terminal-lifecycle compatibility rule
