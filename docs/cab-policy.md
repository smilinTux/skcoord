# CAB policy

`skcoord.itil.CAB_POLICY_ENABLED` is the single setting for the estate-wide Change Advisory Board gate. It defaults to `False` as of 2026-08-29.

Disabling CAB does not delete `cab-decisions/`, alter immutable change cores, or rewrite `cab_required`. Existing records retain that birth fact and fold an explicit `cab-retired` timeline entry. Vote files remain readable audit history, but votes no longer approve, reject, or block a change.

## Controls that remain mandatory

A change tagged `destructive` or classed `risk=high` cannot enter `implementing` unless all of these are true:

1. `test_plan` states the fail-closed preflight to run.
2. `rollback_plan` is non-empty.
3. The latest append-only `preflight` event passes.

A failed preflight must include a reason. The fold records it and refuses the transition before mutation. A later attempt requires a newer passing preflight event.

Chef explicit approval remains an authority requirement at the actuation boundary. Disabling CAB does not grant an agent live execution authority.

## Re-enabling CAB

Set `CAB_POLICY_ENABLED = True` in `src/skcoord/itil.py`. No schema or data rebuild is required. Existing vote records are already retained and the original authenticated vote fold remains in place.

Enabling CAB adds one thing beyond preflight: an independent authenticated reviewer can reject the proposed change or approve it after review. Preflight answers whether declared safety checks pass. CAB answers whether a second authorized person agrees the change should proceed. On a one-operator estate where proposer and voter are effectively the same actor, that extra vote has no independent failure mode and remains disabled.
