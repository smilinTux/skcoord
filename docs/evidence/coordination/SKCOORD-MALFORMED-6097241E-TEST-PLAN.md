# Offline malformed CardStore test plan

Card `2e7b4c91` pins prerequisites for the source-only repair tracked by card
`6f4a9c21`. The fixtures in `tests/fixtures/skcoord-6097241e/` are offline
copies. Tests must never point at `~/.skcapstone`, mutate CardStore, or invoke a
claim, release, scheduler, worker, service, or deployment command.

The immutable source revision and every fixture hash are recorded in
`SKCOORD-MALFORMED-6097241E-FIXTURE.json`.

## Required focused tests

1. `test_malformed_physical_lines_fail_closed`
   - Copy `malformed-range.jsonl` into a temporary CardStore only.
   - Assert the first physical line is blank and all remaining 54 physical
     lines fail independent JSONL parsing.
   - Assert the fold reports an integrity error and does not return a card that
     a selector can classify as scheduler-ready.
2. `test_writer_sequence_gap_fails_closed`
   - Copy `authoritative-native.jsonl` into a temporary CardStore only.
   - Assert the writer stream advances from sequence 1 to sequence 3.
   - Assert the fold reports an integrity error and scheduler readiness is
     false.
3. `test_authoritative_fail_is_joined_separately`
   - Parse the native verdict event at physical line 3.
   - Join that evidence with structural lifecycle state using the production
     evidence interface.
   - Assert the evidence verdict is exactly `FAIL` and scheduler readiness is
     false. Never derive this verdict from column state or legacy links.
4. `test_clean_valid_event_control`
   - Copy `valid-control.jsonl` into a temporary CardStore only.
   - Assert every physical line parses, sequences are contiguous, and the fold
     has no integrity error.
   - Supply otherwise scheduler-ready structural and evidence inputs, then
     assert the selector returns ready. This distinguishes integrity rejection
     from blanket rejection.

Every test must use `tmp_path` and must assert that the live source hashes in
fixture metadata remain unchanged. The repair implementation should add these
cases to the existing CardStore and scheduler truth suites without changing the
fixture bytes.
