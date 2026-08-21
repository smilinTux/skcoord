# ADR 0002: CHI node census and CMDB discovery contract

Status: accepted

## Decision

The operator-reviewed fleet Node objects under
`~/.skcapstone/fleet/objects/node/*.json` are the authoritative machine-readable
CHI discovery census. `cmdb reconcile --network` resolves only this set. Scan
output, SSH configuration, DNS, Tailscale, the peer directory, and service
health are evidence about declared nodes; none may silently add a node to
scope.

Every in-scope Node spec carries:

- canonical `name`, normalized `spec.aliases`, and `spec.address.hostname`;
- non-secret `spec.addresses` with address kind and value;
- `spec.os`, `spec.runtime`, and an operator-owned `spec.role`;
- `spec.reachability` with state and observation time;
- `spec.provenance`, naming each source used for review; and
- `spec.cmdb` with `scope`, `lifecycle`, and any unresolved discrepancy.

The 2026-08-21 census uses `chiap09` as the canonical asset name and records
the live hostname `chioc09` as an alias. `chipv05` is an in-scope virtualization
host. The Windows and WSL identities of `chiwk12` are one workstation asset
with multiple addresses; network collection targets its Linux/WSL SSH alias.
`chiwk11` remains in scope but its runtime role is explicitly deferred, so a
collector may report partial coverage without removing the node. The retired
Windows-era `chiap06` record and unrelated tailnet devices are out of scope.

## CI identity and fields

`make_ci_id(type, canonical_name)` is the deterministic ID function. Supported
types are `host`, `service`, `agent`, `port`, `datastore`, `network`, `device`,
and credential **metadata** (`credential`). A CI observation must retain its
canonical name, node, collector/source authority, observed-at time, scan ID,
observed-versus-declared state, and normalized attributes. Credential CIs may
record only identifiers, issuer/backend class, rotation or expiry metadata,
and owning service; secret values and private paths are prohibited.

Relationships use the controlled vocabulary `runs_on`, `hosts`, `depends_on`,
`connects_to`, and the identity-migration-only `alias_of`. Relationship targets
must exist and `cmdb audit` must pass before an applied baseline is accepted.

## Source precedence and lifecycle

Precedence is:

1. reviewed fleet Node spec for scope, canonical identity, role, and aliases;
2. live SSH hostname and OS/runtime observation;
3. DNS and Tailscale identity/address state;
4. SK peer directory, service health, and declared registries.

Lower sources may add provenance or expose drift; they do not overwrite a
higher-authority identity. A complete scan refreshes `last_seen`. Evidence
older than the four-hour fleet freshness SLO is stale, not absent. Retirement
requires three complete, checksum-valid passes with the same target/collector
scope. Partial, unreachable, permission-limited, or tool-missing scans never
increment absence or retirement state.

## Security and operational consequences

The census contains inventory metadata, never passwords, tokens, environment
values, private keys, key contents, or inline SSH options. Network runners use
one explicit `skvault://` credential reference per in-scope target. Missing
references, incomplete collection, scope drift, backup evidence, or the three
shadow artifacts block apply even when a change is approved.

`chiap09` remains read-only while its dirty-worktree remediation is open, and
`chiwk11` remains discovery-only until runtime-role qualification completes.
These containment states are census metadata, not reasons to omit the assets.

