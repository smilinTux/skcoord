import json

import pytest
from pydantic import ValidationError

from skcoord.card import Card, Column, Kind
from skcoord.scheduler_truth import (
    BlockerCategory,
    EligibilityReason,
    SchedulerBlockerV1,
    SchedulerOutcome,
    SchedulerTruthSnapshotV1,
    SchedulerTruthV1,
    WorkClass,
    canonical_labels_for_write,
    classify_structural_cards,
    normalize_scheduler_outcome,
    snapshot_json,
)


def card(card_id: str, **changes) -> Card:
    values = {
        "id": card_id,
        "kind": Kind.TASK,
        "title": card_id,
        "status": Column.BACKLOG,
        "swimlane": "feature",
    }
    values.update(changes)
    return Card(**values)


def test_snapshot_has_exclusive_primary_reasons_and_overlap_facets():
    snapshot = classify_structural_cards(
        [
            card("done", status=Column.DONE),
            card("ready", dependencies=["done"]),
            card("review", status=Column.REVIEW),
            card("multi", labels=["human-gate", "not-claimable"]),
            card("blocked", dependencies=["missing"]),
        ]
    )

    assert snapshot.population == snapshot.ready + snapshot.excluded == 5
    assert snapshot.ready == snapshot.implementation + snapshot.review == 2
    assert sum(snapshot.exclusive_counts.values()) == snapshot.population
    assert snapshot.exclusive_counts == {
        EligibilityReason.DEPENDENCY_UNKNOWN: 1,
        EligibilityReason.EXPLICIT_CLAIM_DENIAL: 1,
        EligibilityReason.READY: 2,
        EligibilityReason.TERMINAL_DONE: 1,
    }
    assert snapshot.overlap_counts[EligibilityReason.HUMAN_GATE_PENDING] == 1
    by_id = {item.card_id: item for item in snapshot.cards}
    assert by_id["ready"].structural_leaf is True
    assert by_id["ready"].structural_reason is EligibilityReason.READY
    assert by_id["ready"].primary_reason is None
    assert by_id["review"].diagnostic_facets == (EligibilityReason.AWAITING_REVIEW,)
    assert by_id["multi"].reason_codes == (
        EligibilityReason.EXPLICIT_CLAIM_DENIAL,
        EligibilityReason.HUMAN_GATE_PENDING,
    )


def test_legacy_label_aliases_read_identically_and_future_writes_are_canonical():
    legacy = classify_structural_cards(
        [card("legacy", labels=["not-claimable", "sprint-container"])]
    ).cards[0]
    canonical = classify_structural_cards(
        [card("canonical", labels=["do-not-claim", "parent-container"])]
    ).cards[0]

    assert (
        legacy.structural_reason
        == canonical.structural_reason
        == EligibilityReason.CONTAINER
    )
    assert legacy.diagnostic_facets == canonical.diagnostic_facets
    assert canonical_labels_for_write(
        [
            "not_claimable",
            "sprint-container",
            "Human_Gate",
            "Team_Label",
            "team-label",
        ]
    ) == ("do-not-claim", "human-gate", "parent-container", "team-label")


def test_snapshot_json_round_trip_is_versioned_deterministic_and_read_only():
    cards = [card("b"), card("a")]
    source = json.dumps([item.model_dump(mode="json") for item in cards])
    payload = json.loads(snapshot_json(source))

    assert payload["schema_version"] == "scheduler-truth-snapshot.v1"
    assert [item["card_id"] for item in payload["cards"]] == ["a", "b"]
    assert (
        SchedulerTruthSnapshotV1.model_validate(payload).model_dump(mode="json")
        == payload
    )

    with pytest.raises(ValueError, match="JSON array"):
        snapshot_json("{}")


def test_snapshot_rejects_false_partition_counts():
    snapshot = classify_structural_cards([card("ready")])
    broken = snapshot.model_dump()
    broken["population"] = 2

    with pytest.raises(ValidationError, match="population"):
        SchedulerTruthSnapshotV1.model_validate(broken)


def test_blocked_outcome_requires_exact_typed_evidence():
    blocker = SchedulerBlockerV1(
        category=BlockerCategory.DEPENDENCY,
        referents=("card:deadbeef",),
        evidence_sha256="a" * 64,
    )
    truth = SchedulerTruthV1(
        card_id="example",
        lifecycle=Column.BACKLOG,
        terminal=False,
        work_class=WorkClass.IMPLEMENTATION,
        structural_leaf=True,
        structural_eligible=True,
        scheduler_ready=False,
        structural_reason=EligibilityReason.READY,
        primary_reason=EligibilityReason.BLOCKED_UNCHANGED,
        reason_codes=(EligibilityReason.READY,),
        outcome=SchedulerOutcome.BLOCKED,
        blocker=blocker,
    )
    assert SchedulerTruthV1.model_validate_json(truth.model_dump_json()) == truth

    with pytest.raises(ValidationError, match="requires typed blocker"):
        SchedulerTruthV1(
            card_id="bad",
            lifecycle=Column.BACKLOG,
            terminal=False,
            work_class=WorkClass.IMPLEMENTATION,
            structural_leaf=True,
            structural_eligible=True,
            scheduler_ready=False,
            structural_reason=EligibilityReason.READY,
            primary_reason=EligibilityReason.BLOCKED_UNCHANGED,
            reason_codes=(EligibilityReason.READY,),
            outcome=SchedulerOutcome.BLOCKED,
        )


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("PASS", SchedulerOutcome.PASS),
        ("PASS evidence_sha256=abc", SchedulerOutcome.PASS),
        ("pass_for_review", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_INDEPENDENT_REVIEW | evidence", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_REREVIEW", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_INDEPENDENT_REREVIEW", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_INTEGRATION", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_QUALIFICATION", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_ASSEMBLY", SchedulerOutcome.PASS_FOR_REVIEW),
        ("PASS_FOR_REVIEW_R", SchedulerOutcome.PASS_FOR_REVIEW),
        (
            "PASS_FOR_REVIEW_PACKET_ONLY_EXECUTION_UNAUTHORIZED",
            SchedulerOutcome.PASS_FOR_REVIEW,
        ),
        ("PASS_FOR_INDEPENDENT_REVIEW_ONLY_", SchedulerOutcome.PASS_FOR_REVIEW),
        ("BLOCKED", SchedulerOutcome.BLOCKED),
        ("BLOCKED_FAIL_CLOSED blocked_on=human", SchedulerOutcome.BLOCKED),
        ("fail: tests red", SchedulerOutcome.FAIL),
        ("WORKER_DIED", SchedulerOutcome.WORKER_DIED),
    ],
)
def test_legacy_verdict_aliases_normalize(legacy, expected):
    assert normalize_scheduler_outcome(legacy) is expected


@pytest.mark.parametrize("value", ["DONE", "APPROVED", "review says PASS", ""])
def test_legacy_verdict_parser_does_not_infer_from_lifecycle_or_prose(value):
    assert normalize_scheduler_outcome(value) is None


@pytest.mark.parametrize("value", ["PASS_FOR_PRODUCTION", "PASS_FOR_DELETION"])
def test_unknown_provisional_pass_aliases_fail_closed(value):
    assert normalize_scheduler_outcome(value) is None


def test_runtime_reason_is_separate_from_structural_reason():
    structural = classify_structural_cards([card("ready")]).cards[0]
    assert structural.structural_reason is EligibilityReason.READY
    assert structural.scheduler_ready is None
    assert structural.primary_reason is None

    runtime = structural.model_copy(
        update={
            "scheduler_ready": False,
            "primary_reason": EligibilityReason.HOST_PINNED_ELSEWHERE,
        }
    )
    assert SchedulerTruthV1.model_validate(runtime.model_dump()) == runtime

    with pytest.raises(ValidationError, match="uncomposed runtime"):
        SchedulerTruthV1.model_validate(
            {**structural.model_dump(), "primary_reason": EligibilityReason.READY}
        )
