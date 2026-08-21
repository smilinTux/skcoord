# Changelog

All notable changes to `skcoord` are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html). The git tag IS the
version (setuptools-scm); a push to `main` cuts the next patch tag (see
`.github/workflows/publish.yml`), and pushing a `v*` tag directly releases it.

## [Unreleased]

### Fixed

- Preserve reviewed terminal change history across CAB provenance upgrades.
  A dry-run-by-default, append-only migration binds the immutable change core,
  legacy vote, and implementing/deployed/verified/closed event prefix to a
  SHA-256 marker; only a valid marker restores `closed`. Raw status approval,
  incomplete/PIR-less histories, forged markers, and unauthenticated creator
  self-votes remain fail-closed. New change cores carry a CAB provenance schema
  version and are categorically ineligible for the legacy migration path.

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

### Fixed
- **The release model in `publish.yml` matches what the repo documents (CMDB-7).**
  PR #4 switched the trigger to tags-only but left the `tag` job in place — and
  that job only runs on a push to `main` — so the documented "a push to the
  default branch cuts the next patch tag" model (pyproject, this file) was dead
  code: merging to main no longer released anything, which is how the #14 merge
  sat unpublished on 2026-08-17. The main branch is back in the trigger, and
  `pypi-publish` now runs only on the tag-push path, so a main push cuts the
  tag and verifies the build (twine check) while exactly one run — the tag
  push it creates, or a manually pushed tag — uploads to PyPI. No more dead
  job, no more double-upload race, and the docs are true again.
- **CMDB reconcile now derives CIStatus from observed state.** Discovery-owned
  CIs could read `degraded` on the headline status field while their own
  `active_state` attribute said `active`, because `seed_from_inventory` derived
  status from open ITIL incident severity and `reconcile` only wrote attributes
  and relationships. Deployed via the same path as the incident version.

  Precedence rule (CMDB-6): observed systemd state is authoritative for
  discovery-owned CIs. `active_state=active` reads operational, `failed` reads
  down; inactive/activating are ambiguous (oneshots, timers), so no status is
  forced and any existing value is left alone. ITIL incident severity stays
  informational via `impact_analysis` and never overrides an observed state on
  the CIStatus field. A manually retired CI is never un-retired by reconcile.
- **`reconcile_from_legacy()` no longer un-completes work.** It converges the
  store ONTO legacy, which was safe before the Phase-4 read cutover and is not
  safe now: the board is served FROM the store, so legacy is a projection that
  lags, and a card completed in the store but not yet reflected in legacy is
  indistinguishable from real drift. Converging such a card moved it out of
  `done`, and the parity gate then went green *because* the completion had been
  destroyed. Observed live on card `b24c71b5` (2026-08-17), whose store held
  `claim -> move -> complete` while legacy still said `ready`/`lumina`. Not
  hypothetical and not manual: the parity soak runs `reconcile --apply` every
  four hours, and card `70dad715` carries reconcile-written events dragging it
  `ready -> backlog` twice across two days.

  A card whose store state is `done` is now skipped **whole** and returned in
  `skipped_uncomplete`, rather than partially converged. Rewriting its owner
  while leaving its status alone would leave the card in a state neither side
  ever held and still would not converge parity. `allow_uncomplete=True` opts
  back in.

  Deliberately interim. Which side is authoritative for status/owner after the
  read cutover is a design decision (card `be8d5561`); un-completing work is not
  something a drift-repair tool should do silently whichever way that lands.

### Added
- **`skcoord.discovery`: CMDB discovery over declared and observed fleet state.**
  `cmdb.seed_from_inventory()` hardcoded three hostnames and scraped the rest of
  its service list out of ITIL incident `affected_services`, so the CMDB could
  only ever describe the fleet someone had already typed into it: 48 CIs, none
  from a scan. The new collectors read the sources we actually keep (fleet
  objects, the service registry, agent homes) plus real machine state (systemd
  services and timers, docker containers, listening sockets). A first live run
  on `.158` found 442 CIs against the 48 stored.

  Every `DiscoveredCI` records whether a fact was **declared** (a spec claims
  it) or **observed** (a machine answered), because a CMDB fed only
  declarations cannot tell you it is wrong. `drift()` is the report that only
  exists because the two are kept apart: `declared_not_observed`,
  `observed_not_declared`, `stored_not_discovered`.

  Machine access goes through a `CommandRunner` (local or ssh), so the same
  collectors run against a remote node and against canned output in a test.
  `reconcile()` is additive only: it creates, updates attributes and
  relationships, and reports CIs it no longer sees as orphans. It never
  deletes, because a collector that silently failed would otherwise erase
  inventory. Decision recorded in `adr/ADR-002-cmdb-canonical-store.md`.

### Fixed
- **Ephemeral listening ports accreted as permanent CIs.** The port collector
  recorded every socket in LISTEN, including the random high ports that RPC,
  mDNS, tailscale and short-lived servers bind. The CMDB is append-only and the
  reconcile cron runs every three hours, so each reboot's fresh set of high
  ports would be created forever while the previous set turned into permanent
  orphans. Observed on `.158`: 15 such CIs after a single day, one already
  orphaned. Ports inside the host's ephemeral range are now skipped, and the
  range is read from the host being scanned (`ip_local_port_range`) rather than
  hardcoded, so a tuned node and a remote ssh scan both use the right bounds.
  The skipped count is logged, because a host silently reporting fewer ports
  than it has is the same failure as reporting none.
- **Drift accused healthy services, three ways.** Getting the report from 372
  findings down to 87 took three fixes, all of which had it crying wolf:
  `merge()` collapsed declared and observed into one flag, so a service that
  was both correctly declared *and* running came out looking undocumented;
  only `.service` units were collected, so every fleet cronjob (a `.timer`) and
  every `runtime: docker` service read as missing; and
  `systemctl show '*.service'` matched only 78 of 211 loaded units, because the
  glob expands against active units, leaving everything inactive-but-loaded
  unclassified and reported. Units are now named explicitly in the lookup,
  classified by `FragmentPath` so distro units are excluded, and `not-found`
  units (referenced by a dependency, never installed) are dropped as the
  dangling references they are. Origin stays three-valued: a failed lookup
  marks a unit `unknown` and still reports it, so a broken lookup shows up as
  noise rather than as a suspiciously clean report.
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
