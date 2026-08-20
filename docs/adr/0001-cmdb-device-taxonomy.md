# ADR 0001: CMDB device taxonomy

Status: accepted

## Decision

Use `host` only for a compute node with an explicit fleet lifecycle. Use
`device` for a network appliance, printer, IoT endpoint, hypervisor-adjacent
appliance, or other fingerprinted endpoint whose presence was observed but
whose ownership and management were not declared.

Fingerprinting does not establish ownership. Device CIs therefore start with
`managed=false` and the tags `unmanaged` and `fingerprint-only`. They may
participate in connectivity and impact relationships, but discovery must not
infer deployment, patch, or retirement authority from their presence.

## Identity and migration

Every host or device has a `canonical_name` and a set of normalized `aliases`.
Aliases may include SSH aliases, DNS hostnames, addresses, fleet object names,
and Proxmox guest names. Discovery merges overlapping aliases deterministically
and reconciliation reuses an existing matching CI ID. IDs are never rewritten.

The six existing fingerprint-only host records should be observed through
`device_from_fingerprint()`. If an existing record's alias matches, reconcile
updates that record in place; otherwise it creates a device CI. Promotion to a
managed host requires a fleet Node declaration and an operator-reviewed
`alias_of` migration relationship. This avoids silently claiming management or
destroying relationship history.

## Consequences

Device lifecycle and staleness remain observation-driven. A missing or partial
scan is not evidence of retirement or failure, and stale/unknown evidence is
kept separate from `operational`/`down` health.
