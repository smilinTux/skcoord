# Security Policy

`skcoord` is the coordination core: the conflict-free board, the ITIL store, and
the card model the rest of the fleet reasons about. It holds **no credentials**,
but it is the integrity boundary for task state, so a bad write here propagates
to every node over Syncthing.

**Maturity-tier:** operational. **Canonical-home:** this file.

## Reporting a vulnerability

Report privately. Do **not** open a public issue for a security bug.

- **Preferred:** GitHub private vulnerability reporting for this repo
  (`Security ▸ Report a vulnerability`).
- **Alternate:** a PGP-encrypted report to the SKWorld security contact via
  CapAuth identity, or the smilinTux / SKWorld maintainers through the SKCapstone
  coordination channel.

Include the affected version, a reproduction, and the impact observed.

## Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| Card state | a concurrent write dropping non-model keys | every mutation goes through `_write_task_raw`, which loads the **raw dict** (not the `Task` model, so keys like `meta.autopilot` survive), mutates, and replaces atomically |
| Card state | two writers racing | single-writer is a hard precondition; the autopilot writer is pinned to one node. **Do not relax that pin.** |
| Status truth | a forged completion | task files carry no status field. Claim/complete live in the calling agent's own file, so an agent cannot mark work done on another's behalf |
| Audit trail | silent history loss | `close_task_obsolete` / `mark_decomposed` record machine-readable blocks **plus** a human-readable `notes[]` line; reversible and auditable |
| Fleet propagation | a new field breaking older nodes | every field is additive and optional; readers must tolerate absence |

## Secret handling

**This repo stores no secrets and must never store one.** Credentials live in the
operator's environment or the KeePass vault (`skvault`, master password
PGP-sealed). Card `meta` is replicated across the fleet in cleartext, so **never
put a credential on a card**.

`.github/workflows/secret-scan.yml` runs the **gitleaks binary** on every push
and pull request, over the full history, and fails the build on a finding. The
binary rather than `gitleaks-action`, because that wrapper needs a paid licence
for organization-owned repos and exits before scanning anything: a check that
cannot scan is worse than no check, since a permanently red gate gets ignored.

If a secret ever lands: rotate first (new credential live and proven before the
old one is revoked), verify with a call that genuinely authenticates rather than
a list endpoint that ignores the header, and do not allowlist a real finding.

## Dependency posture

- Runtime dependencies are declared in `pyproject.toml`.
- `skcoord` is import-time independent of `skcapstone`: a one-way dependency
  pinned by `tests/test_smoke.py::test_imports_do_not_pull_skcapstone`.
- The version is derived from the git tag (setuptools-scm). A hardcoded version
  drifts and rebuilds an already-published release; do not add one.

## What this repo does NOT claim

- The board is conflict-free for the write patterns it defines (per-agent files,
  atomic raw-dict card writes). It is **not** a general-purpose CRDT and offers
  no guarantee under concurrent writers to the same card from multiple nodes.
