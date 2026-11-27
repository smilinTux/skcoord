# Task 00bb0a6a TDD: Two-store graph truth

## Problem

SKCoord has two append-only sources with different authority:

- `cards/<id>/core.json` and `cards/<id>/events/*.jsonl` hold lifecycle,
  dependency, claim, void, and archive facts.
- `coordination/card_events/*.jsonl` holds the legacy annotation overlay,
  including links, labels, verdict evidence, hashes, gate status, review
  results, and supersession references.

Reading only one source has caused false dependency cycles, unenforced
blocking claims, and DONE being reported as PASS. Link and label commands can
also report success after writing only the legacy overlay.

## Contract

Add one deterministic, read-only joined reader in SKCoord. For one card it
returns:

- folded lifecycle status, effective dependencies, active claim, void, and
  archive state from the CardStore;
- current labels and links plus categorized verdict, hash, gate, review, and
  supersession annotations;
- raw provenance that says whether each annotation exists in the per-card
  authoritative stream, the legacy overlay, or both;
- no derived review verdict. DONE is lifecycle only, and links never become
  dependency edges.

Add a bounded board audit with deterministic card and finding order. It
reports:

1. probable fence annotations whose key or value asserts dependency, block,
   cycle, gate, or fence semantics without a matching effective dependency,
   active claim, void, archive, or recognized claim-gating label;
2. legacy verdict-bearing evidence with no matching authoritative per-card
   event;
3. authoritative verdict-bearing evidence with no matching legacy overlay
   event.

The audit is read-only and reports total populations separately from the
bounded emitted findings.

## Mutation protocol

Use one SKCoord helper for CLI and MCP link and label writes:

1. append a per-card CardStore event;
2. construct a new CardStore reader and read back the exact event ID and
   payload from disk;
3. append the legacy overlay event;
4. construct a new overlay reader and read back the exact action and payload;
5. return success only after both independent readbacks match.

If any step fails, raise loudly. Since append-only writes cannot be deleted,
an exception after step 1 explicitly reports authoritative partial state.
Retry is safe because link and label folds are idempotent even if provenance
contains a repeated event.

## Test plan

SKCoord tests:

- joined COMPLETE plus BLOCKED remains verdict BLOCKED;
- joined COMPLETE plus PASS reports explicit PASS evidence only;
- DONE without verdict has no verdict;
- legacy-only `95e192fd` BLOCKED evidence is a projection gap;
- the `2a9fad93` and `645d53d4` false-cycle annotations are flagged;
- the `f1e3e96b` stale-execution fence is flagged;
- a genuine dependency edge is not flagged;
- audit snapshots prove no source bytes or mtimes change;
- exact authoritative and overlay annotation readback succeeds and injected
  readback or overlay failures fail closed.

SKCapstone tests:

- CLI and MCP link and label calls use the same verified helper and produce
  equivalent joined state;
- CLI and MCP truth and audit output serialize the same SKCoord contracts;
- no success response is emitted when authoritative readback fails.

## Rollback

Code rollback removes the joined reader, audit, and CLI/MCP surfaces. Existing
new per-card link and label events are harmless foldable duplicates of legacy
annotations. No dependency event is created, removed, or reversed by this
change.

## Repair addendum 3b93f388

This repair is bound to predecessor review `d23aa1df`, evidence SHA-256
`da8bccee2651ab782fbd46f2f64f891d8bcf16bea596d695dbe8962c61c8ae54`, and
the frozen candidate manifest SHA-256
`4f53b6b969a2cd6f73918f0883b184445e1cd3b9f8d0eba4d6593696b481e7b1`.
It changes only the reviewed joined-truth core boundaries.

### Repaired read and audit contract

- Fold legacy links and labels globally by `(ts, writer, seq)`, regardless of
  event-file enumeration order.
- Report labels as current per-store records. Each label record exposes
  authoritative and legacy presence independently, plus whether either store
  has an explicit current removal. The convenience `labels` list is the union
  of currently present per-store values and never hides disagreement.
- Treat a fence as enforced only by a mechanism specific to that annotation:
  all card IDs asserted by a dependency fence must be effective dependencies;
  claim, void, archive, and recognized label assertions require that exact
  current mechanism. An unrelated dependency, claim, void, archive, or label
  cannot suppress a finding.
- Recognize explicit verdict tokens under the actual legacy keys `disposition`,
  `result`, `closure-state`, `independent_review`, `gate_status`, and `review`,
  as well as the original explicit verdict keys. Lifecycle DONE and prose with
  no explicit verdict token remain non-verdict evidence.

### Repaired mutation protocol

The existing per-card mutation lock covers the authoritative append, fresh
authoritative readback, overlay append, and fresh overlay readback. A caller
may supply a stable transition ID for retry. The corresponding overlay event
stores that identity in a new optional `event_id` field; old overlay records
without it remain readable. Success requires the exact event ID, writer,
action, payload, and operation identity from independent readers. A retry
finds and verifies either an already complete pair or repairs an
authoritative-only pair without duplicating the authoritative event.

### Repair tests

- Reproduce all five unrelated-mechanism false negatives and the exact live
  incident shapes, including a claimed card with unrelated dependencies.
- Reverse file-name order relative to event time for both links and labels.
- Cover initial labels, additions, removals, cross-store disagreement, and
  deterministic serialized provenance.
- Parameterize all actual verdict keys and explicit PASS, BLOCKED, FAIL,
  FAIL_CLOSED, HOLD, and CHANGES_REQUIRED values; reject DONE and vague prose.
- Prove lock acquisition, same-card serialization, different-card progress,
  concurrent identical operations, stable retry after partial state, exact
  overlay identity, wrong-writer substitution rejection, old-event parsing,
  unknown-card rejection, meaningful removal, independent readback failures,
  and no false success.
- Prove bounded deterministic findings, complete population counts, repeated
  serialization, and audit source-byte and mtime immutability.

## Coreless no-write repair addendum 094a49e0

Adapter review `7dcffbb9`, evidence SHA-256
`609e1de4a2546df9e367c1172dea98a0c1dfd773ef3e795d3203e9b906fb3af6`,
found that the corrected helper created a per-card lock path before rejecting
an unknown card. The repair performs a read-only foldable-core preflight before
lock acquisition. `CardStore.append_event` retains its validation inside the
lock so the preflight does not replace the race defense.

Unknown-card link, add-label, and remove-label tests compare the complete
storage snapshot before and after rejection. The SKCapstone CLI and MCP guard
tests must also pass with this repaired source overlaid, proving that rejected
calls create no lock directory, lock file, card directory, authoritative
event, or legacy overlay event.

## Raced-invalid no-artifact repair addendum e146e2ef

Independent rereview `63971b3b`, evidence SHA-256
`03c3f3828a36e12843641ee25cc65c396f83e0b26f74a153778df23a6570cd9c`,
proved that a core removed after preflight but before persistent lock creation
still left coordination lock artifacts. The helper now requests an
artifact-neutral card lock. This lock opens and exclusively locks the existing
core inode before validation and mutation; all ordinary card locks use the same
core anchor before their existing persistent lock, preserving same-card
serialization and compatibility. If a persistent per-card lock already exists,
the artifact-neutral path opens and honors it without creating, changing, or
deleting lock state.

The inside-lock `append_event` foldable-core validation remains mandatory. A
raced missing or malformed core therefore rejects before event-directory or
overlay creation while the helper itself creates no node. Deterministic tests
inject core loss and malformed JSON at the actual preflight-to-lock boundary
for link, add-label, and remove-label, compare every filesystem node and its
metadata against the externally changed tree, exercise concurrent stable
unknown calls, and prove both core-anchor and pre-existing-lock serialization.
