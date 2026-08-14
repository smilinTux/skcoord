# Changelog

All notable changes to `skcoord` are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html). The git tag IS the
version (setuptools-scm); a release is cut by pushing a `v*` tag.

## [Unreleased]

### Added
- **Full SK_REPO_DOC_STANDARD doc set.** `SOP.md` (9 sections, architecture
  diagram, and an executed `docs-evidence` block of 10 hermetic drift checks),
  `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`, plus a `docs-check` CI gate
  (`.github/workflows/docs-check.yml`, tiers 1 and 2) wired to the shared
  sk-standards validator.
- `secret-scan` CI gate running the **gitleaks binary** over the full history.
  Not `gitleaks-action`: that wrapper requires a paid licence for
  organization-owned repos and exits before scanning anything, which produced a
  permanently red check elsewhere in the fleet that scanned zero bytes.

### Fixed
- **README described `SKCOORD_CARD_STORE` backwards.** It read as an opt-in gate
  ("gated by `SKCOORD_CARD_STORE`"), but `card_store_read_enabled()` /
  `card_store_write_enabled()` have been default-ON since Phase 4e: the store is
  disabled only by an explicit `0` / `off` / `false` / `no`. The README now
  describes it as the kill switch it is, and states that the CardStore root is
  `~/.skcapstone/cards/`, a sibling of `coordination/` rather than a child.

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
