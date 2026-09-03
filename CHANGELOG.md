# Changelog

All notable changes to `skcoord` are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html). The git tag IS the
version (setuptools-scm); a push to `main` cuts the next patch tag (see
`.github/workflows/publish.yml`), and pushing a `v*` tag directly releases it.

## [Unreleased]


### Added

- SKCOORD-OPERATOR-PRINCIPAL-01: immutable `operator_principal` provenance
  records for candidate authors, recommenders, reviewers, and integration
  actors (`src/skcoord/operator_principal.py`). Records live in the separate
  evidence store (`coordination/card_events/*.jsonl`) joined with - never
  inferred from - the structural CardStore events, and carry only identity
  facts (principal, actor identity, optional subject); no credentials or
  personal secrets are stored.

- Reviewer independence gate: assignment, independent review, and merge
  eligibility fail closed when the author and reviewer resolve to the same
  operator principal across different identities, hosts, sessions, or forge
  accounts (default alias table covers chef, jarvis, Lumina, Mero families).
  Backwards compatibility is explicit and conservative: a card with no
  principal records stays merge-eligible (legacy); once the gate is adopted
  (at least one record exists), a missing role or principal fails closed.

### Changed

- Split the CMDB discovery implementation into focused collector,
  reconciliation, scan, drift, and shared-model modules while preserving
  `skcoord.discovery` as the compatibility import surface.

### Fixed

- Fixed three CMDB drift-matcher false-positive classes responsible for 23 of
  27 "high severity" findings in the 2026-08-23 audit: `collect_cron_jobs` now
  parses `sk-cron-run.sh <name>` wrapper invocations (fixing an inline
  `VAR=value` parsing bug that silently dropped those lines) and carries the
  declared job name as an alias; Operatorapp-kind CIs (CLI tools, not
  daemons) are now preserved with a `fleet_kind`/`cli` marker and exempted
  from `drift()`'s declared-not-observed running-unit check; and
  `collect_fleet_objects`/`collect_registry` now carry `spec.unit` and
  registry `pid_file` stems as declared aliases. `drift()` now matches on the
  full alias set (`_service_keys()`) instead of only `.name`.
- Limited automatic CMDB incident routing to CIs carrying the explicit
  `alert-on-drift` tag, preventing generic inventory drift from creating
  unreviewed incident floods.

- Reverted the stale coordination rewrite in `c5731be`, restoring canonical
  agent projection validation, transactional lifecycle repair, ownerless
  ready/doing claims, and fail-closed dependency enforcement. The existing
  claim API already accepted `force=True`, so no compatibility behavior was
  lost.

- Fold acceptance-criteria amendments into the authoritative CardStore
  projection and task views while keeping `core.json` birth facts immutable.
  The legacy kill switch now preserves criteria on projected cards, and
  rollback export writes the current folded criteria for store-born tasks.

- Preserved the terminal change lifecycle across the CAB provenance upgrade
  (docs/adr/0002). Votes recorded before authenticated
  `subject_role`/`subject_fingerprint`/`authorization_id` fields existed
  demoted already-closed historical changes (chg-a76c0aee, chg-1dc7aa09) back
  to `reviewing` at fold time. An unprovenanced approval vote decided before
  the fixed past cutoff `_LEGACY_CAB_PROVENANCE_CUTOFF` is now honored as a
  historical human approval — a schema-derived, fold-time-only rule that
  rewrites no history and fabricates no vote. The raw-status CAB bypass guard
  stays fail-closed: no vote written after the cutoff can satisfy the legacy
  clause, and new changes still require authenticated provenance.

- Stopped promoting every observed host interface address into a CMDB identity
  alias. Reused container-bridge addresses caused a ten-node CHI scan to merge
  all hosts into one CI; addresses remain provenance-rich attributes while
  governed names and explicit aliases control identity reconciliation.

- Added a validated optional SSH port to the skvault metadata adapter and
  strict SSH runner, allowing WSL targets on port 2222 without inline options
  or reliance on ambient SSH configuration.

- Separated the canonical target identity from an optional validated SSH
  transport hostname so dual Windows/WSL nodes remain one CI while collection
  reaches the reviewed WSL endpoint.

- Publish the auto-tagged release to PyPI in the same GitHub Actions run. Tags
  pushed by the workflow's `GITHUB_TOKEN` do not trigger a second workflow, so
  the former tag-only publish guard produced green builds and tags through
  `v0.1.15` while PyPI remained at `0.1.8`.

- Restored the missing `Board.set_grade()` write/return after adding optional
  session and node provenance. The interrupted edit previously built the grade
  document but never persisted it, breaking coordination grading at runtime.

- Made bounded CMDB deadline handling portable across supported Python
  versions by catching `concurrent.futures.TimeoutError` explicitly; Python
  3.10 no longer leaks a deadline exception instead of returning an incomplete
  fail-closed scan result.

- Recorded the verified three-shadow CMDB scope, authenticated Chef approval,
  exact ATLAS network unit contract, freeze/report-only cutover sequence, and
  rollback acceptance checks in the operational rollout SOP.

## [0.1.5] - 2026-08-14

### Added
- **Cross-run failure memory (storage half).** `Board.record_attempt()` and
  `Board.clear_attempts()` maintain `meta.autopilot.attempts[]`, a sibling of
  `scores[]` / `edits[]`.

  A terminal non-pass of an autopilot build previously died with the run:
  in-run feedback never left `run()`, so the next run of the same card rebuilt
  into the identical wall. The card now carries one distilled line per terminal
  failure, which skharness reads back at run start.

  - `record_attempt(task_id, run_id, round, outcome, tried, why_failed, replacement_hint="")`
    is **idempotent on `(run_id, outcome)`**: a retried finalize or a
    crash-resume replaces the entry in place instead of double-counting one
    failure. Storage is capped at 10 entries as a corruption guard, which is
    distinct from the reader's context policy.
  - `clear_attempts(task_id) -> list[dict]` wipes the array and **returns** the
    removed entries rather than writing a journal. skcoord has no journal code
    and the autopilot run journal is skharness-owned, so the caller archives
    them. This preserves the "skcoord stores facts, skharness decides" split.

  Additive-only: one new optional array written exclusively through
  `_write_task_raw`, so it cannot clobber sibling keys and needs no migration
  across the Syncthing fleet. Cards predating the field behave exactly as before.

  Spec: `skharness/docs/specs/2026-08-14-skharness-failure-memory.md`.
  Tests: `tests/test_failure_memory.py` (9 cases, TDD).

## [0.1.4] and earlier

Not retrofitted. See `git log` and the release tags for history predating this
changelog.

[Unreleased]: https://github.com/smilinTux/skcoord/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/smilinTux/skcoord/releases/tag/v0.1.5