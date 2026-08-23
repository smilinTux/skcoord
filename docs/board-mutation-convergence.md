# Board mutation convergence

Local board mutations use a bounded advisory lock order:

1. shared board-mutation lock
2. affected per-card locks in lexical card-ID order
3. lifecycle-only lock when a lifecycle operation needs it

Claims lock both the target and any card displaced from `current_task`.
Claims, completions, releases, stale releases, and lifecycle transitions all
follow this order before changing an agent projection.

`flock` coordinates processes only on one local filesystem. It is not a
cross-host distributed lock and must not be represented as one. Cross-host
convergence is append-only: each event folds by `(ts, writer, seq)`, and a
`release_claim` event names both its expected owner and the exact
`claim_revision`. A release whose precondition no longer matches is a
deterministic fold conflict and leaves the newer owner untouched. The fold
records that conflict in card metadata for audit.

A different-owner claim that reaches an already active card is also an
explicit fold conflict. The original owner remains effective and completion
is blocked until an attributable release or reconciliation resolves the
conflict. This gives cross-host writers deterministic convergence without
misrepresenting local `flock` as a distributed mutex.

When a CardStore write reports an error after the legacy agent projection has
changed, it first searches for its deterministic transition ID. If the exact
event is durable and folds to the intended owner and column, the mutation is
successful despite the reported exception. If the event is missing or folds to
a conflict, the board first fsyncs a recovery intent, restores the raw legacy
bytes with a no-follow temporary file and atomic replacement, then fsyncs a
recovery outcome. Recovery records retain the affected transition IDs for
auditable reconciliation.

Task-label mirrors use the same rule. A failed add or remove label mirror
restores the exact task bytes and compensates already-applied label events. A
durable label event that reports an error remains successful because both task
and CardStore folds have reached the intended label set.
