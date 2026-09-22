# bf712d72 activation result

Date: 2026-09-22

Disposition: **BLOCKED, safely rolled back**

## Bound inputs

- SKCoord reviewed commit: `64e8bfa9b668f7727030798e72fde44cbd42bc0a`,
  tree `97cbd11b3074dad6a971e8e581502e124351c9ba`.
- SKCapstone reviewed commit:
  `a4d46e4b4ab299862af47a96559789856f9cb587`, tree
  `1ac3020bfd6758def598f23b5ecc3401ec7a88ac`.
- Independent review SHA256:
  `8fe3472262efb2f1852e966874c6e2d280c5e7b56c566dbe16b130631b0ce9d9`.
- Activation preflight SHA256:
  `4dde43fdb880cc35895b1b31ae0fda4086c5a3fcbf4c1561eadbfab6b5bd10b5`.
- Exact activation TDD SHA256:
  `0422f425e3027292f503b3d4a3977fc41041a50a277c8805dfde4e0c4430ab02`.

## Completed activation steps

The exact reviewed source was built into two local wheels:

- SKCoord wheel SHA256:
  `71ac2eb4ec32474807f3c88450a77bb246066267aaf1e5c85e6d810b0c0f1b13`.
- SKCapstone wheel SHA256:
  `79d65448ba082bf34a8472424960d76f5c9f35d579cfddd0f61810677402f7f2`.

Before installation, the installed environment was archived to a mode `0600`
rollback archive with SHA256
`741f3caa50547d05df980fbc01cc0854b0d41afc0db4361caa8fa6dbb0786eaf`.
The archive contained 1,344 members. `tar --compare` returned zero differences.

The Jarvis daemon, every recorded active SKFleet timer, the mail acknowledgment
service, and 41 old `skcapstone-mcp` processes were stopped. One short-lived
Seraph coordination writer appended two valid events during the first
observation, so that observation was rejected. After that writer stopped, two
observations five seconds apart were identical:

- shard SHA256:
  `73920a3b59104ec9ec6bd0f95b8b2507dd991dc93b2d729320a7352f76388c07`;
- line count: `73197`;
- inode: `43845669`;
- size: `26116742` bytes;
- line 19797: `589` bytes, SHA256
  `521dea6229a40e43d35ec12b9399f6d836ecad9a8c8d440d0b0df1b118c554a6`.

The stable shard was preserved in the evidence directory. The exact wheels
installed successfully. Installed recovery source files compared byte-for-byte
with both reviewed commits, and the supported
`skcapstone coord recover-overlay` command was available.

## Fail-closed result

The supported plan command exited nonzero before creating a plan or changing
the ledger:

```text
Error: repaired overlay shard remains invalid at line 10977: ValueError
```

The exact stderr artifact SHA256 is
`98f75467b932ca48fdea2120e6688f2151ab90055cf151ae0f09f1f0ea055436`.
The reviewed recovery contract intentionally refuses to remove line 19797 when
another rejected row would remain. Card `bf712d72` authorizes only the pinned
line 19797 recovery, so line 10977 was not inspected, translated, or modified.
No `recover-overlay apply` or ledger rollback command ran. No event JSONL was
edited directly.

## Runtime rollback

The candidate packages were uninstalled through pip and the exact preimage
archive was restored while writers remained stopped. Full recorded file hashes
passed, the archive comparison returned zero differences, and the installed
versions returned to:

- SKCoord `0.1.81`;
- SKCapstone `0.15.168.dev301+gfd7e46f2`.

The overlay remained byte-exact at SHA256
`73920a3b59104ec9ec6bd0f95b8b2507dd991dc93b2d729320a7352f76388c07`.
The Jarvis daemon, all previously active timers, and the mail acknowledgment
service were restored active.

Read-only fold verification still reports card `086ea05c` as `done` with
the trusted `BLOCKED` capability verdict. The rejected PASS-shaped row
remains excluded and still emits the known line 19797 warning.

## Required next repair

A new exact recovery scope must diagnose the supported line 10977 rejection,
bind its bytes and provenance, and authorize an ordered recovery that leaves
the entire shard strict-valid. It should use the same reviewed recovery
primitive and preserve each rejected row separately. This is a newly discovered
integrity defect, not an added human approval gate.

Full nonsecret evidence is under
`/home/skuser01/.skcapstone/evidence/work/bf712d72/live-recovery/`.

