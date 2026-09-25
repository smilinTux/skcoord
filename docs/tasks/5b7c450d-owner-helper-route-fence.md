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

## Independent review and repair (FAIL_SOURCE_ONLY)

Independent source review (`/home/skuser01/.skcapstone/evidence/work/5b7c450d/INDEPENDENT-REVIEW.md`, SHA256 `87c9b4d88d7f22e58c98d34d469ef3e054acc91f3c4a3820b8c19f0e0dfbc5a5`) rejected candidate `5ca1b08` (tree `410113446cf173deb871f9af863cd97b131d8a58`) with two blockers:

1. `set(task.tags)` compared against the required set collapsed duplicate supported route labels, so a helper carrying two identical route tags (e.g. two `sk-m` labels) was accepted. The repair counts exact occurrences: the selected route must occur exactly once in `task.tags` (counting `_is_route_label` variants that normalize to the selected route), and a second distinct exact supported route tag fails. No `set()` is used for the occurrence count.
2. Inherited route labels were removed only when byte-identical to a supported route. The repair identifies inherited route labels with `label.strip().lower()` against the `sk-s`/`sk-m`/`sk-l`/`sk-xl` set (matching the F3 producer), so padded (` sk-m `), case-variant (`SK-L`), and tab-suffixed (`sk-xl\t`) legacy route labels are removed and replaced with the metadata-selected route tag, while every prefixed (`parent-sk-l`, `inherited-sk-xl`) or unrelated restriction is preserved byte-exact.

Regression coverage added in `tests/test_owner_helpers.py`: duplicate selected route (`["sk-m", "sk-m"]` -> `helper must carry exactly one occurrence of the selected route tag`), duplicate other exact route (`["sk-s", "sk-m"]` -> `helper must not carry a second exact route tag`), and padded/case/tab inherited route variants (`" sk-m "`, `"SK-L"`, `"sk-xl\t"`) with prefixed lookalikes preserved. All claim/revision, parent-contract digest, source-binding, dependency, scope-overlap, and replay fences remain enforced. The repair was committed as a new child commit (not an amend): `fix(coord): reject ambiguous helper routes`.

## Limitations

This validates helper creation metadata and labels only. It does not select a worker, sandbox execution, publish results, transfer parent custody, provide a distributed lock, or stop a remote worker. The local coordination authority and execution controller retain those responsibilities.

## Rollback

Revert the card commit as one unit. Do not partially remove the metadata field or route-tag validation: callers, validation, tests, documentation, and changelog form one contract. Existing durable helper records are not rewritten by rollback.

## No-install boundary

This is a source-only change. No package installation, runtime mutation, board mutation, cluster resumption, provider change, commit push, or deployment is part of this card.
