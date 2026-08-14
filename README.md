# skcoord

Sovereign multi-agent coordination + ITIL service management, extracted from
`skcapstone` (CR-4.1) as the standalone coordination core. A **pure Python
library**: one runtime dependency (`pydantic`), no CLI, no daemon, no network
surface.

**Maturity-tier:** T0, N/A (no key material, no crypto surface).
**Docs:** [SOP.md](./SOP.md) · [SECURITY.md](./SECURITY.md) ·
[CONTRIBUTING.md](./CONTRIBUTING.md) · [CHANGELOG.md](./CHANGELOG.md)

## What's here

- **`coordination`**: the conflict-free task board (`Board`, `Task`, each agent
  writes only its own files under `~/.skcapstone/coordination/`). Task files carry
  no status field; status is derived.
- **`card`**: the read-only unified kanban `Card` projection over tasks + ITIL,
  plus the per-writer overlay log `coordination/card_events/<host>.jsonl`.
- **`card_store`**: the event-sourced Card store (`~/.skcapstone/cards/<id>/core.json`
  written once, plus append-only per-writer event logs folded on read).
  `SKCOORD_CARD_STORE` is its **kill switch, not an opt-in**: post Phase-4e the
  store is default-ON and is disabled only by `0`, `off`, `false`, or `no`
  (`dual` means write both, read legacy). See [SOP.md section 6](./SOP.md).
- **`itil`**: incident / problem / change / CAB / KEDB service management.
- **`cmdb`**: event-sourced configuration items + relationships.
- **`agent_card`**: the shareable sovereign identity vCard for the mesh.
- **`atomic_io`**: atomic file-write helper shared by the above.

## Dependency direction

Import-time dependencies flow **one way**: `skcapstone` depends on `skcoord`.
The few reverse edges into skcapstone internals (`skjoule`, `active_agent_name`,
`gtd_tools`, `pubsub`, `activity`) are runtime-lazy inside the methods that use
them, so there is no import-time cycle. In `skcapstone`, `skcapstone.coordination`
/ `.card` / `.card_store` / `.itil` / `.cmdb` / `.agent_card` / `.atomic_io` remain
as re-export shims, so every existing importer keeps working byte-identically.

That also means **every skcapstone process runs skcoord code**: the daemon, the
per-agent units, the dashboard, and skoperator all reach it through those shims.

## Install

```bash
~/.skenv/bin/pip install -e .
```

The version is derived from the git tag by setuptools-scm, so a shallow clone
without tags builds a dev version. Use `fetch-depth: 0` and `fetch-tags: true`.

## Test

```bash
~/.skenv/bin/python -m pytest tests/ -q
~/.skenv/bin/python -m ruff check src/ tests/
```

## License

GPL-3.0-or-later. See [LICENSE](./LICENSE).
