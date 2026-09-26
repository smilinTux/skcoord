# SKCOORD focused helper route admission

## Source and base

- Source worktree: `/srv/sklegal-fast/work/skcoord-918e24be`
- Source card: `/home/skuser01/.skcapstone/evidence/work/2bcd7c98/SKCOORD-FOCUSED-ROUTES-TDD.md`
- Source-card SHA256: `2997ce1b169634df188704bce1038d9c06beffecb33efcef6214ed4a35dd2aa4`
- Base revision: `0bfe2f00fa5ab3223ecb0aa026a8933bf899098e`
- Parent blocker: SKFleet F4 card `2bcd7c98`

## Behavior

The owner-helper route fence now admits the canonical focused selector routes
`sk-<family>-<size>` for `family` in `deepseek`, `zai`, `glm`, `codex` and
`size` in `s`, `m`, `l`, in addition to the existing generic `sk-s`, `sk-m`,
`sk-l`, and `sk-xl` routes. Admission and tag matching remain literal: focused
routes are not normalized, so focused XL (`sk-deepseek-xl`), unknown families
(`sk-llama-s`, `sk-ds-s`), unknown sizes (`sk-deepseek-ss`), aliases without a
size (`sk-deepseek`), mixed case (`SK-DEEPSEEK-S`), and padded or embedded
whitespace variants are rejected before any child card is written.

Route-shaped labels are reserved. Any inherited label matching
`sk-[a-z]+(?:-[a-z]+)?` after `strip().lower()` is treated as a route label and
replaced by the metadata-selected route, so an invalid focused label cannot
survive on a child as an unrelated parent restriction. Labels such as
`parent-sk-deepseek-s` and `inherited-sk-codex-l` do not match the reserved
shape and are preserved byte-for-byte. The selected route tag must occur exactly
once and no second distinct route tag may be present; metadata/label mismatches,
duplicates, and ambiguous combinations fail closed.

Source binding, parent claim revision, parent-contract digest, explicit-request
create-once identity, dependency retention, scope-overlap checks, and
no-child-on-failure behavior are unchanged. Card lifecycle, credentials,
provider resolution, fleet selection, and the installed runtime are untouched.

## Verification

Run from the source worktree:

```text
python -m pytest -q -p no:cacheprovider tests/test_owner_helpers.py
python -m ruff check src/skcoord/owner_helpers.py tests/test_owner_helpers.py
python -m black --check src/skcoord/owner_helpers.py tests/test_owner_helpers.py
git diff --check
```

The focused tests cover all twelve admitted focused routes; focused XL, unknown
family, alias, unknown size, mixed-case, padded, and whitespace variants; the
non-string and missing-metadata boundaries; duplicate and mismatched route
labels; preservation of prefixed lookalikes and unrelated restrictions; and the
existing generic route, replay, custody, and no-child guarantees.

## Boundaries

This is a source-only change to `src/skcoord/owner_helpers.py`,
`tests/test_owner_helpers.py`, and this task record. It does not install,
publish, push, deploy, restart, mutate the board or CardStore, call a provider,
or modify the active Python environment.

## Rollback

Revert the card commit as one unit. Do not partially remove the focused-route
admission: validator, tests, and this record form one contract. Existing
durable helper records are not rewritten by rollback.
