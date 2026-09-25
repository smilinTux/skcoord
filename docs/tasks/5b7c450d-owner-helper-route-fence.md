# 5b7c450d — Owner-helper logical route fence

## Source and base

- Source worktree: `/srv/sklegal-fast/work/skcoord-crew-route-meta`
- Recovered preimage worktree: `/srv/sklegal-fast/work/skcoord-d3be5b44-owner-help`
- Installed preimage commit SHA: `fe77c126d8bf535b4b9fb8f2ca9b18d22b175cf1`
- Recovered d3be worktree patch SHA-256 (`git diff --binary HEAD` over its five recovered paths): `537e7ad52cb3f169769b9a65a1a6c2e28413b6d54d5faefb85f069050d4504f1`

The recovered changelog, owner-help contract, and coordination implementation were retained byte-for-byte. This card changes only the helper validator and its tests, and adds this task record.

## Behavior

Owner-created helpers must declare exactly one logical route in metadata and tags. The only supported values are `sk-s`, `sk-m`, `sk-l`, and `sk-xl`. Values are not normalized: missing, null, non-string, padded, case-variant, combined, and unknown values fail before child creation. The selected route tag must exactly match metadata, and multiple exact route tags fail.

Exact inherited route tags are replaced by the selected route. Labels that merely contain a route-like suffix or prefix, including `parent-sk-l` and `inherited-sk-xl`, remain unrelated restrictions and are preserved. Existing owner, claim-revision, parent-contract, explicit-request digest, source binding, dependency, and scope-overlap validation remains enforced.

## Verification

Run from the source worktree:

```text
python -m pytest tests/test_owner_helpers.py -q
python -m pytest -q
ruff check .
ruff format --check .
git diff --check
```

The focused tests cover all four supported routes; missing/null/non-string/padded/case/unknown and combined values; multiple exact route tags; metadata/tag mismatch; preservation of prefixed restrictions; refolding an amended parent before recomputing its contract digest; and no-child failure behavior.

## Limitations

This validates helper creation metadata and labels only. It does not select a worker, sandbox execution, publish results, transfer parent custody, provide a distributed lock, or stop a remote worker. The local coordination authority and execution controller retain those responsibilities.

## Rollback

Revert the card commit as one unit. Do not partially remove the metadata field or route-tag validation: callers, validation, tests, documentation, and changelog form one contract. Existing durable helper records are not rewritten by rollback.

## No-install boundary

This is a source-only change. No package installation, runtime mutation, board mutation, cluster resumption, provider change, commit push, or deployment is part of this card.
