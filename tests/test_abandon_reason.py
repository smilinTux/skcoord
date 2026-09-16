"""The abandon_reason vocabulary is closed and normalising."""

from __future__ import annotations

import pytest

from skcoord.abandon_reason import ABANDON_REASONS, validate_abandon_reason


def test_vocabulary_is_exactly_the_specified_reasons():
    assert ABANDON_REASONS == frozenset(
        {
            "criteria-unsatisfiable",
            "dependency-unsatisfied",
            "capability-missing",
            "error",
            "superseded",
            "unspecified",
        }
    )


@pytest.mark.parametrize("reason", sorted(ABANDON_REASONS))
def test_every_valid_reason_round_trips(reason):
    assert validate_abandon_reason(reason) == reason


def test_case_and_whitespace_are_normalised():
    assert (
        validate_abandon_reason("  Criteria-Unsatisfiable  ")
        == "criteria-unsatisfiable"
    )


def test_unknown_reason_is_rejected_with_the_vocabulary_in_the_message():
    with pytest.raises(ValueError) as excinfo:
        validate_abandon_reason("because-i-felt-like-it")
    message = str(excinfo.value)
    assert "because-i-felt-like-it" in message
    assert "criteria-unsatisfiable" in message


def test_missing_reason_defaults_to_unspecified_and_never_raises():
    """Reaper and sweep paths must always succeed, even without a reason."""
    assert validate_abandon_reason(None) == "unspecified"
    assert validate_abandon_reason("") == "unspecified"


def test_release_claim_without_a_reason_records_unspecified(tmp_path):
    """MUST NOT raise. Dispatcher reaper paths supply no reason and must succeed."""
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    card_id = store.create(CardCore(id="probe01", title="probe card"))

    event = store.append_event(card_id, "release_claim", "worker-1")
    assert event["abandon_reason"] == "unspecified"


def test_release_claim_with_a_valid_reason_is_recorded(tmp_path):
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    card_id = store.create(CardCore(id="probe02", title="probe card"))

    event = store.append_event(
        card_id, "release_claim", "worker-1", abandon_reason="  Dependency-Unsatisfied "
    )
    assert event["abandon_reason"] == "dependency-unsatisfied"


def test_other_actions_do_not_require_a_reason(tmp_path):
    """Only release_claim is constrained. claim and complete are unaffected."""
    from skcoord.card_store import CardCore, CardStore

    store = CardStore(tmp_path)
    card_id = store.create(CardCore(id="probe03", title="probe card"))

    assert store.append_event(card_id, "claim", "worker-1")
    assert store.append_event(card_id, "complete", "worker-1")
