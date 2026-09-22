# f0d0ba98: governed overlay recovery and strict event admission

Bases: SKCoord work branch `work/f0d0ba98-event-recovery` at reviewed exact-stream recovery commit `e5f3c09bdbe2b529b7cbf2fd00811468d751d414` over `v0.1.81`; SKCapstone work branch `work/f0d0ba98-event-recovery` at commit `a368cc7fdb313080dd73d83f1272d9bd10991e79`.

Generalize the exact `38c4a706` evidence-preserving recovery primitive into a supported recovery API for one malformed `coordination/card_events/*.jsonl` row. The operator supplies the shard writer, one-based line number, full source SHA256, exact line SHA256, recovery card ID, evidence destination, and actor. A read-only plan binds those facts and the current schema diagnostic. Apply and rollback consume the plan or receipt rather than rediscovering intent.

Planning must reject healthy rows, ambiguous targets, symlinks, hardlinks, unsafe directories, and non-overlay paths. Apply must hold the overlay writer lock, re-open through no-follow directory descriptors, revalidate the full file, exact line, schema diagnostic, and surrounding content, preserve the complete original shard plus rejected line and intent before replacement, remove only the pinned row by atomic same-directory replacement, fsync all durable state, and emit a hash-bound receipt. Exact retry returns the completed receipt. Rollback must revalidate the repaired shard hash and absence of intervening changes, restore the byte-exact original shard atomically, verify its hash, and preserve all prior evidence.

`CardEvent` must forbid unknown fields. `CardEventLog.append` must validate a nonempty bounded writer identity, a known action, action-specific required and reserved fields, and the existing writer file chain while holding the same writer lock used for append. Malformed or broken existing content fails before any append. Unknown fields, missing required fields, contradictory reserved fields, and unsafe writer-derived paths fail closed.

Focused SKCoord tests must cover plan-only immutability, preservation, apply retry, concurrent append rejection, rollback, symlink and hardlink rejection, wrong-schema rows, writer and reserved-field strictness, locked chain revalidation, and strict per-card fold behavior. The historical `086ea05c` malformed overlay row remains rejected and may only contribute a diagnostic card hint; its actual folded card stays `BLOCKED`, and no recovery code interprets or translates its truncated `PASS` text.

Allowed SKCoord paths are this TDD, `src/skcoord/card.py`, the existing recovery module or one focused replacement module, and focused tests. Allowed SKCapstone paths are its matching TDD, the coordination CLI registration, doctor diagnostics, focused CLI and doctor tests, and one changelog fragment. The supported `skcapstone coord` CLI and read-only doctor consume one public SKCoord recovery and diagnostic API. SKCapstone must not duplicate parsing, custody, locking, replacement, or rollback logic.

Do not inspect or modify live event JSONL, execute a live recovery, expose credentials, install, deploy, push, or alter provider state. Rollback is supported only for the exact shard bound in a valid receipt and is exercised only on synthetic fixtures by this task. Because pre-upgrade appenders lock the replaceable data inode instead of the new stable sidecar lock, live apply requires all writers on the target host to be upgraded or quiesced. The plan records this technical prerequisite and the supported CLI requires an explicit quiescence assertion. Verification uses the isolated SKCoord source through `PYTHONPATH`; it does not install either candidate.

Verification commands:

```text
cd /home/skuser01/work/skcoord-f0d0ba98-event-recovery
python -m pytest tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py -q
python -m ruff check src/skcoord/card.py src/skcoord/card_event_recovery.py tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py
python -m ruff format --check src/skcoord/card.py src/skcoord/card_event_recovery.py tests/test_card_event_recovery.py tests/test_overlay_ledger_integrity.py

cd /home/skuser01/work/skcapstone-f0d0ba98-event-recovery
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m pytest tests/test_coord_event_recovery.py tests/test_doctor.py tests/test_card_events.py -q
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m ruff check src/skcapstone/cli/coord.py src/skcapstone/doctor.py tests/test_coord_event_recovery.py tests/test_doctor.py
PYTHONPATH=/home/skuser01/work/skcoord-f0d0ba98-event-recovery/src:src python -m ruff format --check src/skcapstone/cli/coord.py src/skcapstone/doctor.py tests/test_coord_event_recovery.py tests/test_doctor.py
```
