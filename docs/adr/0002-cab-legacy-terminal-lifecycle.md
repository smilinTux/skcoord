# ADR 0002: Legacy CAB votes and the terminal-lifecycle compatibility rule

Status: accepted

## Context

Commit 63a18b2 ("bind CAB approval to authenticated human roles",
2026-08-20) added `subject_role` / `subject_fingerprint` /
`authorization_id` to `CABDecision` and required an authenticated human
identity or role for an approval to count at fold time. Votes recorded
before that upgrade carry none of those fields. Historical changes that had
validly completed their lifecycle under the pre-upgrade regime (notably
chg-a76c0aee and chg-1dc7aa09, both closed after deployed -> verified ->
closed transitions with PIR notes) demoted back to `reviewing` at fold
time, and every post-approval event folded conflicted. Their history was
valid when written; the regression is in the reader, not the record.

## Decision

Superseded in part by card bada5c86 (2026-11-13): the original timestamp rule
below is replaced by an explicit pinned list. See "Revision: pinned legacy
list".

### Revision: pinned legacy list (card bada5c86)

A vote now counts as a human approval only when it stores a signed CapAuth
grant that verifies against a pinned approver key
(`skcoord.cab_authorization`). Historical votes have no stored grant, so the
fold honors them only through `_LEGACY_CAB_APPROVALS` in `src/skcoord/itil.py`:
a frozen set of (change id, voter, sha256 of the vote file).
`_is_pinned_legacy_approval` matches an approval read from disk whose file hash
equals the pinned one. The hash fixes every field, provenance included, so a
vote that is edited by one byte, written later, backdated or built in memory
never matches.

The list was built from read-only folds of every reachable estate on
2026-11-13: noroc2027 (and its copy on 192.168.0.41) and the chi nodes
(chiap01, chiap02, chiap03, chiap04, chiap08, chiap10, ziowk01; their trees
are identical). It holds exactly the votes whose lifecycles would otherwise
regress:

| Change | Voter | Why |
|---|---|---|
| chg-11d0e1c7 | lumina | verified; its only approval |
| chg-a543c87b | lumina, architect | closed (gate 4 change) |
| chg-a76c0aee | jarvis | closed; pre-upgrade vote (fixture) |
| chg-1dc7aa09 | jarvis | closed; pre-upgrade vote |
| chg-ca4d0ea5 | Casey's owner vote | closed |
| chg-d639f369 | Casey's owner vote | implementing |
| chg-fe7869b5 | Casey's owner vote | implementing |

Casey's three votes carry an `authorization_id` but the signed envelope was not
stored, so they cannot be re-verified; they keep their history only. Chef's
chg-a543c87b vote is in the same position and is not pinned, because the
change stays closed without it. A pinned vote is history: skcapstone's
actuation gates never count one, since they require a verifying grant.

Adding a pin is a code change reviewed like any other. New votes are never
pinned: a vote cast today either carries a grant or does not count.

### Original rule (2026-08, replaced)


An approval vote is honored as a historical human approval when **all** of
the following hold (`_is_legacy_unprovenanced_approval` in
`src/skcoord/itil.py`):

1. All three authenticated provenance fields are empty: the pre-upgrade
   record shape (schema-derived, not writer-claimed).
2. `decided_at` parses as a timezone-aware timestamp strictly before
   `_LEGACY_CAB_PROVENANCE_CUTOFF` (`2026-08-21T00:00:00+00:00`). Naive or
   malformed timestamps fail closed.

The rule is a pure fold-time derivation. Nothing is appended, rewritten, or
backdated; the original vote and event evidence stay byte-identical on disk
and remain the authority the fold derives from. No human vote is fabricated:
the historical jarvis vote already exists and cites the human owner's
authorization in its `conditions`.

## Why new changes could not abuse the original rule

- `submit_cab_vote()` never accepts a `decided_at`; it stamps the
  wall-clock now. The cutoff is a fixed past timestamp, so any vote written
  through the API after the rule ships can never satisfy clause 2. A legacy
  *shape* (empty provenance fields) is necessary but not sufficient.
- The raw-status CAB bypass guard is untouched: a `status -> approved`
  event on an unapproved change still folds conflicted for every live
  event, regardless of this rule.
- Backdating `decided_at` requires direct write access to the
  `cab-decisions/` tree, the same accepted threat level the codebase
  already documents for forging `node="migrated"` on a replayed event.
- The no-self-approval filter (P1.4, drop the drafter's own APPROVE vote)
  runs before `_is_human_approval` and is unaffected; a rejection vote
  still blocks unconditionally.

## Consequences

- chg-a76c0aee and chg-1dc7aa09 fold `closed` again; their post-close
  evidence (duplicate/corrective close events, the codex-root post-close
  note) folds conflicted, so closed stays terminal and no late event
  reopens them.
- (Original rule; the revision above pins Casey's later owner vote on it.)
  chg-ca4d0ea5 (approval vote decided 2026-08-21T15:54Z, after the cutoff)
  deliberately does **not** qualify. Widening the cutoff past "now" would
  open a live bypass window in which a brand-new legacy-shaped vote could
  self-approve. That change needs a genuine authenticated human vote
  (owner/approver role) or an operator decision, not a wider grandfather
  window.
- The fixture `tests/fixtures/itil-terminal-legacy/` is a verbatim
  read-only copy of the live chg-a76c0aee record; history is reproduced,
  never rewritten.
