# 7d38c4a7: exact CardStore stream recovery

## Purpose and binding

Recover only card `38c4a706`, writer file `pi-seraph-38c4a706@chiap08.jsonl`, whose complete original SHA256 is `07fcd8ff2040401a437290fc0575812ae0f50174fcce4104e12aa4a499303600`. This task is bound to SKCoord `v0.1.81` peeled commit `d53d2c0361441c5ebd386eb9fc87ee8ca8863232` and claimed card `7d38c4a7`. The earlier card `f6f38c4a` was voided because its source revision named an annotated tag object rather than the peeled commit.

The first event is a valid claim. Three subsequent events were written outside the CardStore append protocol and have an invalid `prev_hash`; they assert link, PASS verdict, and completion. Those assertions are incident evidence, not accepted review or completion. Recovery must retain the exact first line, preserve all four original lines as evidence, and ensure the three invalid events cannot affect a fold.

The exact core SHA256 is `be18eb005967653fdc5f71c423cab8daa1d8ffa6551c851c6084e6d657713276`. The first raw line including its newline has SHA256 `159e7fa6330ac3a6f30f9d559d88b018b30080cc1471028aa22c981825033977`, event ID `3e9f8c999df64df1b7a0166e888bfc57`, and claim revision `783d389dc9874170ba9260790bb87b2b`. The invalid tail has event IDs `6b86b273ff34fce19d6b804eff5a3f57` (`link`), `d4735e3a265e16eee03f59718b9b5d03` (`verdict`), and `4e07408562bedb8b60ce05c1decfe3ad` (`complete`) in that order.

## Contract

1. A dedicated, mediated command takes the exact card ID, writer name, full source SHA256, valid prefix SHA256, recovery card ID, and evidence destination. It refuses any mismatch and never scans for a different target to repair automatically.
2. Under the common card mutation lock, re-read the source through safe directory handles and validate the complete bytes, regular single-link path, first event identity and chain, and exact selected tail. Refuse symlinks, hardlinks, extra lines, malformed JSON, changed order, or a concurrent append.
3. Before any event-file replacement, durably preserve the original bytes and a provenance manifest outside the live `events/` directory. The manifest names both card IDs, actor, original and retained hashes, event IDs, source and destination, tool revision, time, and disposition. Preserve the original complete stream byte-for-byte.
4. Replace only the pinned writer file with its byte-exact valid first line using an atomic same-directory operation and fsync. Normal CardStore strict readers and appenders remain unchanged. Verify a strict fold of the affected card and then the global board fold. A retry must recognize the exact completed transaction and return its receipt without another replacement.
5. A failure before replacement leaves the original file unchanged. A failure after replacement must be distinguishable by durable intent and byte hashes, and retry must finish verification. Refuse unexpected state. Do not silently accept a second corrupt writer or a conflicting legacy event.
6. Any rollback is a separate exact operation under the same lock. It requires the post-recovery file SHA and no intervening writer append, restores the original bytes atomically, and records a new receipt. It does not erase incident evidence.

## Verification

Focused tests cover the successful exact target, wrong card/writer/hash/prefix/tail, path safety, concurrent append, interrupted transaction and retry, subsequent normal append, independent corruption, and rollback refusal after an intervening append. Source review is independent of the implementer. Live execution requires a reviewed source patch and a preflight that confirms the original SHA still matches. Then run the command once, verify evidence hashes and strict fold, and retry the native review opener. Do not infer review approval from the quarantined events.

## Boundaries

No generic degraded reader, CardStore JSONL shell edit, forged PASS, direct board repair, W4/W6 acceptance, remote push, or product deployment is in this task. A reviewed local maintenance command may be installed and run for this exact stream after independent source review and live preflight. The stale `jarvis` projection for voided `f6f38c4a` is a separate lifecycle defect; this command does not alter it.
