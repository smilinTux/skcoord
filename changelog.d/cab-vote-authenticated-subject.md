### Fixed

- A CAB approval now counts only with authenticated provenance: a human
  approver role (`owner`/`approver`), the signer fingerprint and the signed
  authorization id. A bare `agent="human"` vote, which the skcapstone MCP tool
  wrote whenever CapAuth resolution failed, is still recorded but no longer
  approves a change. Votes by the change's creator, its `managed_by` owner or
  the operator seat's own identity (`operator`, `atlas`, device seats) do not
  qualify either; identities compare by local part, so spellings such as
  `capauth:chef@skworld.io` match `chef`. Historical approvals before the
  2026-08-21 provenance cutoff keep their meaning. New helper
  `_qualifying_approvals` is the single rule both fold sites use (card bada5c86).
