# SchedulerTruthV1 - Canonical Scheduler Truth Contract

## Overview

SchedulerTruthV1 is the versioned SKCoord contract for structural facts and SKCapstone runtime composition. It provides:

- Exclusive primary reasons for scheduler eligibility/ineligibility
- Diagnostic facets for auxiliary context
- Legacy verdict alias support with canonical vocabulary migration
- Population invariants for validation
- Operator action guidance

## Schema Version: skcoord.scheduler-truth/v1

## Contract Separation

### SKCoord (Shared Contracts and Structural Facts)

- **Lifecycle state**: open, claimed, complete, void, archived
- **Terminal disposition**: done, blocked
- **Structural leaf type**: task, review, repair, escalation
- **Dependencies**: Card dependency IDs
- **Labels**: Card labels including human-gate, review, etc.
- **Verdict outcome**: PASS, PASS_FOR_REVIEW, BLOCKED (canonical)
- **Blocker**: dependency, human, capability, card types with referent
- **Human decision**: approval, override, escalation records

### SKCapstone (Live Runtime Facts)

- **Claim state**: owner, revision, timestamp
- **Launch count**: Number of execution attempts
- **Launch backoff**: Expiration timestamp for backoff
- **Worker state**: Live session tracking
- **Host routing**: Which host holds the worker
- **ITIL integration**: Incident/problem/change references
- **Capacity facts**: Slot availability, resource limits

## Primary Reasons (Exclusive)

Every evaluated card has exactly one primary reason. The population equals ready cards plus all primary-reason counts.

### Ready Pool Reasons (scheduler_ready=True)

| Reason | Description | Worker Action |
|--------|-------------|---------------|
| `ready_no_dependencies` | Card has no dependencies and can be claimed | Claim card for execution |
| `ready_dependencies_complete` | All dependencies are complete and card can be claimed | Claim card for execution |
| `ready_human_approved` | Human approval granted and dependencies complete | Claim card for execution |

### Ineligible Reasons (scheduler_ready=False)

| Reason | Description | Worker Action |
|--------|-------------|---------------|
| `blocked_dependency_incomplete` | One or more dependencies are not complete | Do not claim (dependency incomplete) |
| `blocked_human_decision_pending` | Awaiting human approval or decision | Do not claim (awaiting human decision) |
| `blocked_human_decision_denied` | Human decision denied card execution | Do not claim (human decision denied) |
| `blocked_capability_insufficient` | Card requires capability not available to current agent | Do not claim (assign to stronger agent) |
| `blocked_card_unsatisfiable` | Card criteria cannot be satisfied as written | Do not claim (card must be revised) |
| `blocked_terminal_complete` | Card is complete and done | Do not claim (terminal) |
| `blocked_terminal_void` | Card is voided and cannot be executed | Do not claim (terminal) |
| `blocked_terminal_archived` | Card is archived and no longer active | Do not claim (terminal) |
| `blocked_claimed_by_other` | Card is claimed by another agent | Do not claim (respect existing claim) |
| `blocked_launch_backoff` | Card is in launch backoff after failed attempts | Do not claim (in backoff) |
| `blocked_lifecycle_excluded` | Card excluded by lifecycle assessment | Do not claim (excluded by policy) |

## Diagnostic Facets (Non-Exclusive)

Facets provide auxiliary context without affecting the primary decision. A card may have zero or more facets.

### Dependency Facets
- `has_dependencies` - Card has dependencies
- `dependency_cycle_detected` - Dependency cycle found
- `stale_execution_blocked` - Stale execution blocks re-execution

### Human Decision Facets
- `human_gate` - Card requires human gate
- `human_override` - Human override applied
- `escalation_required` - Escalation required

### Execution Facets
- `claimed` - Card is currently claimed
- `owner_unknown` - Claim owner cannot be determined
- `launch_failed` - Launch attempt failed
- `launch_timeout` - Launch attempt timed out

### Quality Facets
- `verdict_pass` - Verdict is PASS
- `verdict_pass_for_review` - Verdict is PASS_FOR_REVIEW
- `verdict_blocked` - Verdict is BLOCKED
- `independent_review_complete` - Independent review is complete

### Historical Facets
- `legacy_only_state` - State exists only in legacy store
- `legacy_verdict_alias` - Verdict uses legacy alias

## Population Invariants

### Invariant 1: Exactly One Primary Reason
Every evaluated card has exactly one primary reason. This is a fundamental contract.

### Invariant 2: Scheduler Readiness Mapping
`scheduler_ready` is True **iff** `primary_reason` is a ready pool reason:
- `ready_no_dependencies`
- `ready_dependencies_complete`
- `ready_human_approved`

### Invariant 3: Population Equality
For any set of evaluated cards:
```
total_cards = count(cards with ready_pool_reason) + sum(count(cards) for each non_ready_reason)
```

## Legacy Support

### Verdict Aliases
Legacy verdict text is normalized to canonical outcomes:

**PASS aliases**: pass, approved, accepted, success, complete, done, landed, merged

**PASS_FOR_REVIEW aliases**: pass for review, pass review, needs review, review required, ready for review

**BLOCKED aliases**: blocked, block, blocked on, depends on, waiting for, awaiting, blocked by

Historical events are never rewritten. New writes use canonical vocabulary.

### Label Support
Legacy labels like `human-gate`, `review`, `high-priority` remain readable and are used for structural leaf inference.

## JSON CLI

### Show Truth as JSON
```bash
skcoord scheduler-truth show-truth <truth-file.json>
skcoord scheduler-truth show-truth <truth-file.json> --pretty
```

### Print Reason Table
```bash
skcoord scheduler-truth reason-table
```

## Example Truth JSON

```json
{
  "schema": "skcoord.scheduler-truth/v1",
  "card_id": "abc123",
  "lifecycle": "open",
  "terminal_disposition": null,
  "structural_leaf": "task",
  "dependencies": ["dep1", "dep2"],
  "labels": ["high-priority"],
  "verdict": {
    "outcome": "PASS",
    "evidence_sha256": "abc123def456...",
    "evidence_path": "~/.skcapstone/evidence/work/abc123/verdict.json",
    "recorded_at": "2026-09-02T00:00:00+00:00",
    "legacy_alias": null
  },
  "blocker": null,
  "human_decision": null,
  "scheduler_ready": true,
  "primary_reason": "ready_dependencies_complete",
  "diagnostic_facets": ["has_dependencies", "verdict_pass"],
  "claim_owner": null,
  "claim_revision": null,
  "claim_timestamp": null,
  "launch_count": 0,
  "launch_backoff_until": null,
  "evaluated_at": "2026-09-02T01:00:00+00:00",
  "card_revision": 5
}
```

## Shadow Mode

The new scheduler truth shadows the existing selector before cutover:

1. **Shadow Comparison**: Both old and new selectors run in parallel
2. **Mismatch Recording**: Differences are logged without changing assignment
3. **Cutover Gate**: Cutover occurs only when unexplained decision deltas are zero
4. **Rollback Path**: Old selector remains active until shadow is validated

## Python API

```python
from skcoord import SchedulerTruthEvaluator, LifecycleState, PrimaryReason

evaluator = SchedulerTruthEvaluator(home=Path("~/.skcapstone"))

truth = evaluator.evaluate(
    card_id="example123",
    lifecycle=LifecycleState.OPEN,
    dependencies=("dep1", "dep2"),
    complete_dependencies={"dep1", "dep2"},
    claim_owner=None,
    launch_count=0,
)

assert truth.primary_reason == PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE
assert truth.scheduler_ready is False
```

## Migration Notes

1. **Historical Events**: Never rewritten. Legacy reads continue to work.
2. **New Writes**: Use canonical vocabulary (PASS, PASS_FOR_REVIEW, BLOCKED).
3. **Backward Compatibility**: Legacy aliases are recognized and normalized.
4. **Gradual Rollout**: Use shadow mode before production cutover.
