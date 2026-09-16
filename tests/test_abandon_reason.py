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
