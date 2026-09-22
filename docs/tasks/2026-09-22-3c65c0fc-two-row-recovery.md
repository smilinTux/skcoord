# 3c65c0fc: ordered recovery of two malformed overlay rows

## Authority and exact inputs

Card `3c65c0fc`, claim revision
`13766588e8fe4244bc977c0a21696be1`, authorizes diagnosis and recovery of
both rejected records in
`/home/skuser01/.skcapstone/coordination/card_events/chiap08.jsonl`.

The implementation starts from the independently reviewed commits:

- SKCoord `64e8bfa9b668f7727030798e72fde44cbd42bc0a`, tree
  `97cbd11b3074dad6a971e8e581502e124351c9ba`;
- SKCapstone `a4d46e4b4ab299862af47a96559789856f9cb587`, tree
  `1ac3020bfd6758def598f23b5ecc3401ec7a88ac`;
- independent review SHA256
  `8fe3472262efb2f1852e966874c6e2d280c5e7b56c566dbe16b130631b0ce9d9`.

The known targets are physical line `10977`, card-pinned SHA256 prefix
`68c15e4e`, and physical line `19797`, exact SHA256
`521dea6229a40e43d35ec12b9399f6d836ecad9a8c8d440d0b0df1b118c554a6`.
A supported read-only scanner must establish the full line 10977 hash,
nonsecret schema diagnostic, card hint, action hint, and common source shard
generation before source design or live apply. Raw JSONL inspection is
forbidden.

## Root cause and required behavior

The reviewed single-row planner correctly fails closed when removing one
target would leave another rejected record. The smallest safe extension is one
atomic, ordered multi-row plan. One sealed plan binds the source shard SHA256
and an ordered list of independently hash-pinned physical lines. Planning
removes all targets from one immutable in-memory source image and requires the
resulting shard to be strict-valid before it writes evidence.

Each target must retain its own line number, SHA256, schema diagnostic, and
separate rejected-row artifact. The plan and receipt must bind the ordered
target list and every artifact. Apply must revalidate the stable source shard
and every target under the existing stable writer lock, replace the shard once,
fsync existing custody evidence and the replacement, and return the completed
receipt on exact retry. Rollback must restore the one byte-exact original
shard only when the repaired hash still matches.

Single-row plans, receipts, apply, retry, and rollback remain compatible.
The CLI accepts repeated line and line-hash pairs while preserving existing
one-row invocation behavior. It rejects missing pairs, duplicate lines,
unordered targets, changed line hashes, unsafe evidence, any unlisted rejected
record, and any strict-invalid resulting shard.

## Test and activation order

1. Add a failing synthetic test with two malformed rows that reproduces the
   current single-row planner refusal.
2. Implement only the multi-row plan, evidence, apply, retry, rollback, and
   repeated CLI option changes required by that test.
3. Run focused SKCoord and SKCapstone suites, Ruff check and format, and the
   existing broad suites named by the reviewed source card.
4. Commit exact source candidates and obtain an independent source review
   against exact commits, trees, diffs, tests, and rollback semantics.
5. Only after PASS, build exact reviewed wheels and a tested installed-runtime
   rollback archive.
6. Announce a coordination write freeze, stop all old appenders, and require
   two identical source shard observations with both line hashes.
7. Install the reviewed wheels, run supported multi-row plan and apply, then
   verify doctor, strict folds, separate custody artifacts, receipt hashes, and
   exact idempotent retry before restarting writers.
8. If another invalid row appears or any postcondition fails, do not broaden
   scope. Keep writers stopped, run supported rollback if apply occurred,
   restore the installed package archive, restore prior unit state, and report
   the exact blocker.

## Boundaries and evidence

Never append, edit, truncate, replace, or delete event JSONL directly. Do not
interpret either rejected record as accepted state, touch unrelated cards,
expose secrets, push source, or deploy to another host. Preserve the full
`bf712d72` evidence unchanged. Store nonsecret diagnostics, source review,
preimages, plan, per-row rejected artifacts, receipt, rollback proof, strict
fold results, and service restoration under
`/home/skuser01/.skcapstone/evidence/work/3c65c0fc/`.

