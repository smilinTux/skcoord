# Pre-Batch Lifecycle Reassessment (PBLR)

Status: design, approved for phased implementation. No component here mutates a
card store until Phase 3, and no class applies automatically until it earns
promotion under section 6.

## Why

Over one night, five classes of card-graph rot each silently blocked work, and no
existing job detected any of them. Portfolio Steward tracks project dependencies;
what failed was card *lifecycle integrity*, which is a different thing.

| class | what it does | measured |
|---|---|---|
| D1 void dependency edges | a voided card left as a dependency blocks dependents forever | 7 repaired, 41 remain |
| D2 stale claims | reviewed-BLOCKED cards left in `claimed` | 3 cards, 93-96h, blocked the whole SKLEGAL critical path |
| D3 unclaimable cards | a failed claim leaves no event, so the card is retried forever | 78 of 162 launches wasted, 48% |
| D4 superseded ranked live | rank read structure, supersession lives in evidence | 1 card ranked highest-leverage while superseded twice |
| D5 volatile CI identity | PID in the CI name mints an incident per restart | 122 of 263 drift incidents, 118 for one service |

## 1. Topology: no authority node

The original design placed the scanner on a single authority node. Rejected: it
centralizes the exact failure it exists to prevent, and when that node is down the
fleet degrades to readmitting the waste.

**Scan is read-only and deterministic over a Syncthing-shared store, so it needs no
authority at all.** Every host runs the full scan independently against its own
local replica. Identical inputs produce identical output; where replicas differ,
they differ only by sync lag, which section 3 handles explicitly. A host gates only
on *its own* scan, so a lagging host is conservative rather than wrong, and a dead
host removes only itself.

**Apply needs no authority either.** It is made safe by two properties rather than
by exclusivity:

1. **Optimistic concurrency.** Before each mutation, re-read the target card's
   event heads. If anything changed since the packet was built, drop the mutation
   and re-scan. A packet is bound to the state it was computed from.
2. **Packet-hash idempotency.** Every applied event records `packet_sha256`. A
   second host applying the same packet sees it already present and no-ops. Double
   apply is detectable and inert, not corrupting.

That is strictly better than partitioning by card-id hash, which would have been
the obvious alternative: partitioning creates orphaned slices when a host is down
and needs lease machinery to recover. Optimistic concurrency plus idempotency
needs neither, and the append-only store already gives us both primitives.

Consequence to state plainly: with no authority node there is no single writer, so
correctness rests entirely on the head re-read and the packet hash. Both are cheap
and both are testable, and Phase 3's exit signal is specifically zero apply-time
head-mismatch surprises.

## 2. Components

- `pblr-scan` read-only joiner and detectors. Emits a validated graph snapshot to
  `coordination/reassessment/snapshots/<node>.json`. Runs on every host.
- `pblr-propose` turns findings into recommendation packets. Files only.
- `pblr-apply` the only writer. Separate binary, separate invocation, separate gate.
- `attempts` store, `coordination/reassessment/attempts/<node>.jsonl`, written by a
  thin wrapper around the rotation's claim call, recording
  `{card_id, ts, node, outcome}`. **This is the missing sensor for D3**: failed
  claims currently leave no trace anywhere, which is why the same card was launched
  eight times. The current launched-twice heuristic in `skfleet-rotate.py` is a
  patch for this absent sensor and is retired once the sensor exists.

Rotation reads its own host's snapshot and draws only from `eligible`. Two dead-man
behaviors: a snapshot older than `2 * scan_interval` degrades to advisory ordering
only, and a missing snapshot falls back to pre-PBLR behavior. **A dead scanner must
never idle the fleet.**

## 3. Reading the two stores

Every detector uses one join, and getting it wrong is how this class of bug is born.

- Structure: `cards/<id>/events/*.jsonl`, keyed `action`. Latest `claim` with no
  later `release_claim | complete | void` means claimed. `complete` and `void` are
  terminal positives.
- Evidence: `coordination/card_events/*.jsonl`, keyed `action` with `link_key`
  facts. **Read it through `evidence_vocab.read_links()`, never by matching
  `link_key` literally.** 3674 distinct keys exist; `human_approval` alone has 26
  spellings and a literal match finds 41 of 173.
- ITIL: `coordination/itil/*/<id>/events/*.jsonl`, keyed **`kind`**, with state set
  only by `kind == "status"`. A card-oriented reader sees no state here at all,
  which is how 269 closed incidents were treated as assignable work.
- **Ordering is `(ts, writer, event_id)`. Never `seq`.** Measured: 145 of 145
  multi-writer cards have two or more files both starting at `seq 0`.
- Free-text `note` values never satisfy a predicate. Prose that claims supersession
  is not an edge.

**Lag horizon H.** Each snapshot embeds a per-node watermark, the newest `ts` seen
in that node's files. `H = max(15 min, 2 * max observed inter-node skew)`. Iron
rule: **absence of an event is meaningful only if the absence is older than H on
every node's replica.** Positive events are always safe to act on. Inside the
horizon a card is `unknown`, which excludes it from `eligible` without raising a
flag. If any node's watermark is older than H, absence-based predicates are
suspended for that cycle and the snapshot is marked `degraded`.

## 4. Detectors

Each detector states its predicate and its failure mode, because a wrong predicate
here can idle or destroy a fleet. That is not hypothetical: a liveness predicate
requiring "pane has a live child" was specified earlier in this estate and would
have reaped the entire fleet, because a Pi TUI is the pane process with zero
children when idle.

**D1 void dependency edges.** `D in deps(X) AND state(D) == void AND no
remove_dependency(X,D)`. Resolve the successor chain to a live terminus before
proposing a rewrite; flag dangling or cyclic chains as their own finding. Failure
mode: removing a real edge lets a dependent run before its prerequisite, which is
why removal is never automatic.

**D2 stale claims, split by evidence.** The split is the whole point, because the
safe remediations are opposite.
- **D2a finished-in-evidence:** `claimed AND verdict present AND review decision
  present`, with the verdict `ts` postdating the open claim. Structure is behind
  evidence. This is bc9ced8e, 2c97a19e, 0e9c18c6.
- **D2b dead worker:** `claimed AND zero evidence since claim AND claim_age >
  worker_max_lifetime * 3 + H AND owner matches the ephemeral worker pattern`.
  Named agents never match.
Failure mode asymmetry drives section 5: auto-releasing a live claim costs one
duplicate run and is recoverable; auto-*completing* a D2a card asserts an outcome
and poisons every dependent. Note that the review decision recorded on those three
cards is literally
`APPROVED_AS_ACCURATE_BLOCKED_OUTCOME_NOT_RUNTIME_APPROVAL`: it says in its own
name that approving the report is not approving progression. Any design that lets
an automatic actor advance state on it has read the decision as the one thing it
explicitly is not.

**D3 unclaimable.** Static branch: the card fails the board's own claim
precondition, evaluated by *importing the board's validator*, not reimplementing
it. Behavioral branch: three or more `claim_rejected` attempts across two or more
nodes or two or more distinct hours, zero claims after the first rejection,
rejection reason not transient. 24h TTL then one probe slot, so exclusion
self-heals. Failure mode: over-exclusion starves work quietly, so the snapshot
reports exclusion-set size and age.

**D4 superseded ranked live.** Not-live when a `superseded_by` link resolves to a
live terminus, or the latest canary verdict is `BLOCKED_FAIL_CLOSED`, or a
credential revision has expired. Rank moves inside PBLR and may read only the
joined record: rank equals the count of **live** transitive dependents. Dangling or
cyclic successor chains keep the card live and warn, because demoting the only live
copy of the work on a broken link is the costlier error.

**D5 volatile CI identity.** Template-cluster test: replace each maximal digit run
with a placeholder; a template is volatile when instances >= 5, exactly one segment
varies, that segment never repeats across instances, and creation correlates with
restarts. The never-repeats condition is what separates `3620007` (a PID, fresh
each restart) from `s102` (a stable ordinal). Incident *closure* is never
automatic; PBLR only marks duplicates in its own snapshot so rotation stops
launching workers at them.

## 5. Auto-apply versus propose

Every auto candidate must pass all four clauses: reversible or purely additive;
asserts no outcome; predicate uses only positive evidence or absence aged beyond H;
idempotent under concurrent runs.

| remediation | lane |
|---|---|
| snapshot exclusion, quarantine, dedup | **auto always** (mutates nothing but PBLR's own output) |
| attempts-store writes | **auto** (new store, touches no card) |
| release stale ephemeral-worker claim (D2b) | **auto-eligible** (reversible, asserts nothing) |
| add-only namespaced labels | **auto-eligible** (additive, removable) |
| complete a D2a card with PASS verdict and approving review | **propose, promotable** |
| any disposition of a D2a card with BLOCKED verdict | **propose, never auto** |
| rewrite a void dep edge to an evidence-linked successor | **propose, promotable** |
| remove a void dep edge with no successor | **propose, never auto** |
| void a superseded card and rewire dependents | **propose, never auto** |
| CI canonical-identity change and incident collapse | **propose, never auto** |

`pblr-apply` hard rules regardless of lane: it refuses any mutation to a card whose
evidence contains a review decision unless the packet quotes that decision verbatim
and the approval references the packet hash. It never writes `verdict`, `review`,
or supersession keys at all; its own annotations use the `x-pblr-` namespace. Every
applied event carries `writer: pblr`, the rule id, and the packet hash.

## 6. Promotion ladder

Every class starts as propose regardless of the table above. A class promotes to
auto only after 20 consecutive human approvals with zero rejections and zero
post-hoc audit failures. One post-promotion audit failure demotes it automatically.
**Auto status is earned by measurement, not argued into existence**, because this
estate's record is that automatic mutation makes things worse.

## 7. Recommendation packet

One file per finding. It carries: the one-sentence claim, the **exact event JSON**
`pblr-apply` would append, verbatim quoted evidence with event ids, the preserved
review decision plus proof the evidence store is untouched, the instantiated
predicate, blast radius, reversibility, confidence with basis, alternatives
rejected with reasons, and a **falsifier**: the single thing a reviewer can grep to
overturn the finding.

**`pblr-apply` executes the falsifier itself at apply time and aborts on a hit.**
This is mandatory, not advisory. A packet designed to be approvable in one read
means a confidently wrong one is approved in one read, so the falsifier must be a
check the machine runs, not prose the human is trusted to run.

## 8. Rollout and kill switch

- **Phase 0 shadow.** Scan only, no rotation coupling, no writes except the attempts
  sensor. Exit signal is reproducing known ground truth: the 41 remaining D1 edges,
  the 3 named D2a cards with review decisions quoted verbatim, the 78-of-162 waste
  re-derived from rotation logs, and the 122-of-263 and 118-same-service D5 numbers.
  Calibrate D1 proposals against what the 7 manual repairs actually chose.
- **Phase 1 advisory.** Rotation uses the snapshot as an ordering hint. Signal:
  wasted-launch rate falls from the 48% baseline with no starvation, tracked as max
  eligible-but-unlaunched age.
- **Phase 2 gating.** Rotation draws only from `eligible`. Signal: wasted launches
  near zero, p95 age of blocked critical-path cards falls, quarantine set stable.
- **Phase 3 proposals.** Packets to the human, applied via `pblr-apply`. Signal:
  zero apply-time head-mismatch surprises.
- **Phase 4 promotion**, one class at a time, D2b first.

**Kill switch.** `coordination/reassessment/DISABLED`, creatable from any host and
carried by Syncthing: scan exits without publishing, apply refuses to run, rotation
reverts on its next tick. Per-class files (`DISABLED.D2b`) demote one class.

## 9. Known weaknesses

- **A bad predicate now runs on every host at once.** Decentralizing removed the
  availability risk, not the correctness risk. Circuit breaker: if a run's eligible
  set shrinks more than 30% versus the prior run, the snapshot self-marks advisory
  and alerts instead of gating. The breaker is itself a risk, since a genuine mass
  supersession would be slow-walked.
- **The join inherits the store's vocabulary sins.** Mitigated by the controlled
  vocabulary landed in `schemas/evidence_vocab.py`, but 42.7% of link events remain
  uncontrolled. A silently renamed key makes D2a blind, which fails safe (cards stay
  quarantined, no packet is produced) but recreates the four-day block.
- **H is estimated, not measured.** If real lag exceeds the horizon, absence-based
  detection quietly stops during exactly the outages that create stale claims. The
  degraded flag surfaces it; nothing fixes it.
- **The ladder can be Goodharted.** Twenty rubber-stamped approvals promote a class
  that never earned it. Random post-approval audits are the mitigation, and audits
  spend the scarcest resource in the loop.
- **Rank is still a heuristic.** Live-dependent count fixes D4's specific lie but can
  still crown a card whose dependents are themselves doomed. It is at least now in
  one place, so the next rank bug has one place to be fixed.
- **New write paths grow the shared folder.** Attempts, snapshots and packets all
  ride the one Syncthing folder and need compaction, or they become the next
  hygiene incident.
