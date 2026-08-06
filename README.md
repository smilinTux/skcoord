# skcoord

Sovereign multi-agent coordination + ITIL service management, extracted from
`skcapstone` (CR-4.1) as the standalone coordination core.

## What's here

- **`coordination`** — the conflict-free task board (`Board`, `Task`, each agent
  writes only its own files under `~/.skcapstone/coordination/`).
- **`card`** — the read-only unified kanban `Card` projection over tasks + ITIL.
- **`card_store`** — the event-sourced Card store (`cards/<id>/core.json` + append-only
  per-writer logs), gated by `SKCOORD_CARD_STORE`.
- **`itil`** — incident / problem / change / CAB / KEDB service management.
- **`cmdb`** — event-sourced configuration items + relationships.
- **`agent_card`** — the shareable sovereign identity vCard for the mesh.
- **`atomic_io`** — atomic file-write helper shared by the above.

## Dependency direction

Import-time dependencies flow **one way**: `skcapstone` depends on `skcoord`.
The few reverse edges into skcapstone internals (`skjoule`, `active_agent_name`,
`gtd_tools`, `pubsub`, `activity`) are runtime-lazy inside the methods that use
them, so there is no import-time cycle. In `skcapstone`, `skcapstone.coordination`
/ `.card` / `.card_store` / `.itil` / `.cmdb` / `.agent_card` / `.atomic_io` remain
as re-export shims, so every existing importer keeps working byte-identically.

## Install

```bash
~/.skenv/bin/pip install -e .
```

## Test

```bash
~/.skenv/bin/python -m pytest tests/ -q
```
