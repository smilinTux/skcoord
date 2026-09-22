# bf712d72: activate reviewed overlay recovery

## Authority and exact inputs

Card `bf712d72` authorizes a bounded local activation of the independently
reviewed recovery implementation and recovery of one malformed overlay row.
The exact reviewed inputs are:

- SKCoord commit `64e8bfa9b668f7727030798e72fde44cbd42bc0a`, tree
  `97cbd11b3074dad6a971e8e581502e124351c9ba`.
- SKCapstone commit `a4d46e4b4ab299862af47a96559789856f9cb587`,
  tree `1ac3020bfd6758def598f23b5ecc3401ec7a88ac`.
- Independent review receipt
  `/home/skuser01/.skcapstone/evidence/work/f0d0ba98/INDEPENDENT-REREVIEW.md`,
  SHA256 `8fe3472262efb2f1852e966874c6e2d280c5e7b56c566dbe16b130631b0ce9d9`.
- Activation preflight
  `/home/skuser01/.skcapstone/evidence/work/f0d0ba98/ACTIVATION-LIVE-RECOVERY-PREFLIGHT.md`,
  SHA256 `4dde43fdb880cc35895b1b31ae0fda4086c5a3fcbf4c1561eadbfab6b5bd10b5`.
- Live target
  `/home/skuser01/.skcapstone/coordination/card_events/chiap08.jsonl`,
  physical line `19797`, exact line SHA256
  `521dea6229a40e43d35ec12b9399f6d836ecad9a8c8d440d0b0df1b118c554a6`.

## Procedure

1. Create isolated worktrees at both reviewed commits. Add and read this exact
   TDD in both repositories before changing runtime state.
2. Build exact local wheels. Record wheel hashes, installed package preimages,
   service and timer state, and a mode `0600` rollback archive containing the
   installed executables, package directories, and matching distribution
   metadata.
3. Announce a coordination write freeze. Stop the Jarvis daemon, every local
   fleet timer and service that can append coordination state, and all old
   `skcapstone-mcp` processes. Require two stable whole-file observations and
   revalidate the target inode, size, line count, line bytes, and line hash.
4. Install the two exact reviewed wheels while writers remain quiesced. Verify
   installed source files byte-for-byte against the reviewed source and verify
   that `skcapstone coord recover-overlay` is available.
5. Run supported `recover-overlay plan` and `apply` for only the pinned writer,
   line, full source hash, line hash, recovery card, and evidence directory.
   Pass `--writer-quiesced` only after the stable observations succeed.
6. Before restarting a writer, run doctor, strict card fold readbacks for
   `086ea05c` and `bf712d72`, receipt hash checks, preserved rejected-byte
   checks, and an exact idempotent apply retry. Card `086ea05c` must remain
   `BLOCKED`; the rejected PASS-shaped row must never become accepted state.
7. Restart only the units that were active before the window. Verify they load
   the installed implementation and verify that the malformed-row warning is
   absent.
8. On any failed postcondition, keep writers stopped, use supported
   `recover-overlay rollback`, restore the package archive if required, verify
   exact preimages, and restore only the recorded prior unit state.

## Boundaries and evidence

Never append, edit, truncate, replace, or delete any event JSONL directly.
The reviewed recovery command is the only permitted ledger mutation. Do not
translate or admit the malformed row, touch unrelated cards, inspect secrets,
push source, or deploy to another host. Store the plan, original shard,
rejected row, receipt, rollback archive hash, before and after package hashes,
unit state, exact commands and exit codes, strict fold results, and final
verification in
`/home/skuser01/.skcapstone/evidence/work/bf712d72/live-recovery/`.

The task may commit this TDD and its nonsecret activation evidence. Completion
requires restored service state, no malformed overlay warning, preserved
provenance, tested rollback capability, and a linked SKCapstone task update.
