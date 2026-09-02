# Scheduler truth contract

`SchedulerTruthV1` keeps lifecycle, outcome, human authority, and eligibility
separate. `done` never implies `pass`. Structural truth never claims runtime
readiness: `scheduler_ready` remains `null` until a runtime adapter supplies
host, capacity, backoff, and live-owner facts.

## Read-only usage

Pass a JSON array of folded `Card` objects on stdin or name a JSON file:

```text
python -m skcoord.scheduler_truth_cli cards.json
```

The command only validates and classifies its input. It does not open or mutate
CardStore, legacy projections, claims, services, or workers.

```json
{
  "schema_version": "scheduler-truth.v1",
  "card_id": "018bf488",
  "lifecycle": "review",
  "terminal": false,
  "work_class": "review",
  "structural_leaf": true,
  "structural_eligible": true,
  "scheduler_ready": null,
  "structural_reason": "ready",
  "primary_reason": null,
  "reason_codes": ["ready", "awaiting_review"],
  "diagnostic_facets": ["awaiting_review"],
  "outcome": null,
  "blocker": null,
  "human_decision": "unknown"
}
```

`structural_reason` is SKCoord's stable structural decision. `primary_reason`
is reserved for the final runtime decision and remains `null` while
`scheduler_ready` is `null`. A runtime adapter may set both together, but it
cannot turn a structurally excluded card into ready work.

Snapshots provide both views operators need:

- `exclusive_counts` uses one precedence-selected structural reason per card and
  always sums to `population`.
- `overlap_counts` counts every structural reason and diagnostic facet, so it may
  exceed `population`.
- `ready + excluded == population` and `implementation + review == ready`.

## Operator actions

| Primary reason | Operator action |
| --- | --- |
| `ready` | No structural repair. Let the runtime adapter evaluate capacity. |
| `malformed` | Repair or quarantine the unreadable card record. |
| `terminal_done`, `archived`, `superseded` | No scheduling action. |
| `owned` | Check the runtime owner lease and worker health. |
| `container`, `non_task`, `state_not_eligible` | No implementation dispatch. |
| `explicit_claim_denial`, `human_gate_pending` | Wait for an explicit human decision. |
| `dependency_unknown` | Repair the missing dependency reference. |
| `dependency_incomplete` | Complete or explicitly amend the dependency. |
| `foreign_project` | Route through that project's scheduler. |
| `awaiting_review` | Assign an independent reviewer. This is a facet of ready review work. |
| `blocked_unchanged` | Wait for the typed blocker generation to change. |
| `host_pinned_elsewhere` | Leave the card for its named healthy host. |
| `sensitive_unapproved` | Obtain exact scoped approval or leave it unclaimed. |
| `capacity_unavailable` | Restore or wait for runtime executor capacity. |

Legacy `not-claimable` and `sprint-container` labels remain valid reads.
`canonical_labels_for_write()` returns `do-not-claim` and `parent-container`
for future events. It never rewrites historical events.

## Runtime composition and cutover

SKCoord owns folded structural classification only. SKCapstone owns runtime
policy such as host pins, sensitive-category approval, worker health, backoff,
attempt limits, and capacity. A runtime adapter may add those facts, but must
preserve the SKCoord source revision and structural decision.

Before cutover, the runtime classifier stays shadow-only. Each cycle compares
the exact population and ready card IDs with the legacy selector and records
exclusive and overlap counts. It must not claim, release, move, label, or launch
from shadow results. Cutover requires a full release window with zero unexplained
population or ready-ID differences, independent review, and explicit activation.
