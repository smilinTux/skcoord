# ADR 0002: Legacy CAB votes and the terminal-lifecycle compatibility rule

Status: accepted

## Context

Commit 63a18b2 ("bind CAB approval to authenticated human roles",
2026-08-20) added `subject_role` / `subject_fingerprint` /
`authorization_id` to `CABDecision` and required an authenticated human
identity or role for an approval to count at fold time. Votes recorded
before that upgrade carry none of those fields. Historical changes that had
validly completed their lifecycle under the pre-upgrade regime — notably
chg-a76c0aee and chg-1dc7aa09, both closed after deployed -> verified ->
closed transitions with PIR notes — demoted back to `reviewing` at fold
time, and every post-approval event folded conflicted. Their history was
valid when written; the regression is in the reader, not the record.

## Decision

An approval vote is honored as a historical human approval when **all** of
the following hold (`_is_legacy_unprovenanced_approval` in
`src/skcoord/itil.py`):

1. All three authenticated provenance fields are empty — the pre-upgrade
   record shape (schema-derived, not writer-claimed).
2. `decided_at` parses as a timezone-aware timestamp strictly before
   `_LEGACY_CAB_PROVENANCE_CUTOFF` (`2026-08-21T00:00:00+00:00`). Naive or
   malformed timestamps fail closed.

The rule is a pure fold-time derivation. Nothing is appended, rewritten, or
backdated; the original vote and event evidence stay byte-identical on disk
and remain the authority the fold derives from. No human vote is fabricated:
the historical jarvis vote already exists and cites the human owner's
authorization in its `conditions`.

## Why new changes cannot abuse it

- `submit_cab_vote()` never accepts a `decided_at`; it stamps the
  wall-clock now. The cutoff is a fixed past timestamp, so any vote written
  through the API after the rule ships can never satisfy clause 2. A legacy
  *shape* (empty provenance fields) is necessary but not sufficient.
- The raw-status CAB bypass guard is untouched: a `status -> approved`
  event on an unapproved change still folds conflicted for every live
  event, regardless of this rule.
- Backdating `decided_at` requires direct write access to the
  `cab-decisions/` tree — the same accepted threat level the codebase
  already documents for forging `node="migrated"` on a replayed event.
- The no-self-approval filter (P1.4, drop the drafter's own APPROVE vote)
  runs before `_is_human_approval` and is unaffected; a rejection vote
  still blocks unconditionally.

## Consequences

- chg-a76c0aee and chg-1dc7aa09 fold `closed` again; their post-close
  evidence (duplicate/corrective close events, the codex-root post-close
  note) folds conflicted, so closed stays terminal and no late event
  reopens them.
- chg-ca4d0ea5 (approval vote decided 2026-08-21T15:54Z, after the cutoff)
  deliberately does **not** qualify. Widening the cutoff past "now" would
  open a live bypass window in which a brand-new legacy-shaped vote could
  self-approve. That change needs a genuine authenticated human vote
  (owner/approver role) or an operator decision, not a wider grandfather
  window.
- The fixture `tests/fixtures/itil-terminal-legacy/` is a verbatim
  read-only copy of the live chg-a76c0aee record; history is reproduced,
  never rewritten.
