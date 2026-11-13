### Fixed

- A CAB approval now counts only when the vote stores the signed CapAuth
  authorization envelope it was cast under, and that envelope verifies against
  a pinned human approver key in `~/.config/skcapstone/cab-approvers.json`
  (new module `skcoord.cab_authorization`). The pin, not the caller's CapAuth
  profile or `CAPAUTH_HOME`, decides who may approve; no pin file means no
  approval. The grant must bind this change, this decision, the vote's role,
  issuer and fingerprint. Provenance fields without a verifying envelope, and
  a bare `agent="human"` vote, are recorded but never count. Votes by the
  change's drafter, creator, `managed_by` owner, the operator seat
  (`operator`, `atlas`, device seats) or a placeholder voter do not qualify;
  identities compare by local part. `submit_cab_vote` takes `authorization=`
  and refuses to replace a signed vote with an unsigned one. The historical
  compatibility rule is now an explicit pin of four votes by change id, voter
  and file sha256 instead of a backdatable timestamp. The "Approved by"
  timeline note names only the qualifying voters (card bada5c86).
