### Added

- `skcoord.startup_evidence.validate_startup_evidence` checks a host-independent,
  hash-bound startup evidence report (card, agent and claim generation must
  match, and a stale report is refused). `Board.claim_task` accepts optional
  `startup_evidence` and `claim_generation` arguments and rejects a failing
  report before taking any mutation lock, so a rejected worker consumes no
  slot and changes no ownership. Callers that pass no evidence are unchanged.
