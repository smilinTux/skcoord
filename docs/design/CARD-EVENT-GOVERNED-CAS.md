# Governed CardEvent compare-and-append

Card `ee19f561` adds a source-only CardEvent write boundary. It is disabled by
default and this candidate does not activate it.

## Boundary

`GovernedCardEventConfig` names one authority node, one authority epoch, and an
exact pre-activation journal baseline. When enabled:

1. Link writes are accepted only when the local node is the named authority.
2. Link writes and conditional transition writes append to the authority
   journal while holding the same exclusive file lock.
3. Every new physical record receives an immutable `event_id`.
4. `append_if_link_revision` scans every known journal while holding that lock,
   returns the original receipt for an exact retry, rejects transition ID reuse,
   and rejects a stale expected link revision without appending.
5. Governed link revision order follows physical authority-journal order. A
   later governed verdict supersedes an earlier verdict even if its supplied
   timestamp sorts earlier.

Historical CardEvent JSON remains readable. Missing governance fields stay
`None`; no historical journal is rewritten.

## Transition identity

`derive_card_event_transition_id` hashes this versioned tuple:

```text
(
  "skcoord.card-event-transition",
  "v1",
  target_card_id,
  verdict_event_id,
  action,
  label,
  marker_payload,
)
```

PR 290 can use the verdict CardEvent `event_id` as `verdict_event_id`, then pass
the resulting transition ID and the same verdict event ID to
`append_if_link_revision`. The same verdict and effect resolve to one physical
marker. A new verdict CardEvent has a new `event_id` and therefore a new
transition ID.

## Audit and fail-closed behavior

`capture_activation_baseline` records the byte length and SHA-256 digest of
each existing journal prefix. `audit_governed_writes` verifies those prefixes
and checks every later link or transition record for the configured authority
journal, node, epoch, and physical event identity. Any violation reports
`available=false`; subsequent governed writes raise
`CardEventAuthorityUnavailableError`.

There is no fallback from a governed write to the legacy per-host append path.
Ordinary CardEvent behavior remains unchanged while governed mode is disabled.

## Rollback

This source candidate has no migration and performs no activation. Reverting
its source commit removes the new API without changing any existing journal.

If a separately approved deployment later activates the boundary, rollback
must first disable its governed writer configuration. Do not delete or rewrite
CardEvent records. Existing readers ignore the added optional fields, so the
append-only evidence remains readable after rollback.
