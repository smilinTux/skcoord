# SKCoord helper route compatibility

Card `b36dba96` extends the owner-helper route fence with exact focused routes
for DeepSeek, Z.ai, GLM, and Codex at S, M, and L sizes. Generic S, M, L, and
XL routes remain accepted.

The metadata route must be an exact string and the helper must carry exactly
one matching route label. Unknown families, XL focused routes, wildcards,
missing values, non-strings, padding, case variants, duplicate labels,
mismatches, and multiple routes fail before child creation. Route-shaped
inherited labels are reserved and replaced by the selected exact route.
Unrelated parent restrictions and prefixed lookalikes remain byte-exact.

Source binding, current parent ownership and claim revision, contract digest,
dependencies, helper identity, request digest, replay, allowed-path overlap,
and atomic no-child-on-failure remain unchanged.

This is source-only. It does not install or activate SKCoord or SKFleet.
Rollback is a source revert of the card commit.
