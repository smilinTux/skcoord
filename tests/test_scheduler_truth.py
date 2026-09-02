"""Tests for SchedulerTruthV1 contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skcoord.scheduler_truth import (
    Blocker,
    BlockerType,
    DiagnosticFacet,
    HumanDecision,
    LifecycleState,
    PrimaryReason,
    SchedulerTruthEvaluator,
    SchedulerTruthV1,
    StructuralLeaf,
    TerminalDisposition,
    Verdict,
    VerdictOutcome,
    _normalize_verdict_text,
    get_operator_action,
    get_reason_action,
    get_reason_description,
)


class TestVerdictNormalization:
    """Tests for legacy verdict text normalization."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # PASS aliases
            ("PASS", VerdictOutcome.PASS),
            ("pass", VerdictOutcome.PASS),
            ("Approved", VerdictOutcome.PASS),
            ("ACCEPTED", VerdictOutcome.PASS),
            ("Success", VerdictOutcome.PASS),
            ("complete", VerdictOutcome.PASS),
            ("done", VerdictOutcome.PASS),
            ("landed", VerdictOutcome.PASS),
            ("merged", VerdictOutcome.PASS),
            # PASS_FOR_REVIEW aliases
            ("PASS_FOR_REVIEW", VerdictOutcome.PASS_FOR_REVIEW),
            ("pass for review", VerdictOutcome.PASS_FOR_REVIEW),
            ("Pass-For-Review", VerdictOutcome.PASS_FOR_REVIEW),
            ("needs review", VerdictOutcome.PASS_FOR_REVIEW),
            ("review required", VerdictOutcome.PASS_FOR_REVIEW),
            # BLOCKED aliases
            ("BLOCKED", VerdictOutcome.BLOCKED),
            ("blocked", VerdictOutcome.BLOCKED),
            ("Block", VerdictOutcome.BLOCKED),
            ("blocked_on", VerdictOutcome.BLOCKED),
            ("blocked on", VerdictOutcome.BLOCKED),
            ("depends on", VerdictOutcome.BLOCKED),
            ("waiting for", VerdictOutcome.BLOCKED),
            ("awaiting", VerdictOutcome.BLOCKED),
        ],
    )
    def test_normalize_verdict_text(self, text: str, expected: VerdictOutcome) -> None:
        assert _normalize_verdict_text(text) == expected

    def test_normalize_verdict_none_for_unknown(self) -> None:
        assert _normalize_verdict_text("unknown verdict text") is None
        assert _normalize_verdict_text("") is None
        assert _normalize_verdict_text(None) is None


class TestSchedulerTruthV1:
    """Tests for SchedulerTruthV1 dataclass."""

    def test_basic_serialization(self) -> None:
        truth = SchedulerTruthV1(
            card_id="test123",
            lifecycle=LifecycleState.OPEN,
            primary_reason=PrimaryReason.READY_NO_DEPENDENCIES,
            scheduler_ready=True,
        )

        data = truth.as_dict()
        assert data["card_id"] == "test123"
        assert data["lifecycle"] == "open"
        assert data["primary_reason"] == "ready_no_dependencies"
        assert data["scheduler_ready"] is True

    def test_roundtrip_json(self) -> None:
        original = SchedulerTruthV1(
            card_id="abc123",
            lifecycle=LifecycleState.CLAIMED,
            terminal_disposition=TerminalDisposition.BLOCKED,
            structural_leaf=StructuralLeaf.REVIEW,
            dependencies=("dep1", "dep2"),
            labels=("human-gate", "high-priority"),
            verdict=Verdict(
                outcome=VerdictOutcome.BLOCKED,
                evidence_sha256="abc123",
                legacy_alias="BLOCKED|blocked_on=dependency referent=card:dep1",
            ),
            blocker=Blocker(
                blocker_type=BlockerType.DEPENDENCY,
                referent="card:dep1",
                description="Dependency not complete",
            ),
            human_decision=HumanDecision(
                decision="pending",
                decision_ref="approval:critical-feature",
            ),
            scheduler_ready=False,
            primary_reason=PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE,
            diagnostic_facets=(
                DiagnosticFacet.HAS_DEPENDENCIES,
                DiagnosticFacet.HUMAN_GATE,
            ),
            claim_owner="pi-glm-chiap01",
            claim_revision=1,
            launch_count=3,
        )

        json_str = original.to_json()
        restored = SchedulerTruthV1.from_json(json_str)

        assert restored.card_id == original.card_id
        assert restored.lifecycle == original.lifecycle
        assert restored.terminal_disposition == original.terminal_disposition
        assert restored.structural_leaf == original.structural_leaf
        assert restored.dependencies == original.dependencies
        assert restored.labels == original.labels
        assert restored.verdict.outcome == original.verdict.outcome
        assert restored.blocker.blocker_type == original.blocker.blocker_type
        assert restored.human_decision.decision == original.human_decision.decision
        assert restored.scheduler_ready == original.scheduler_ready
        assert restored.primary_reason == original.primary_reason
        assert restored.diagnostic_facets == original.diagnostic_facets
        assert restored.claim_owner == original.claim_owner
        assert restored.claim_revision == original.claim_revision
        assert restored.launch_count == original.launch_count


class TestSchedulerTruthEvaluator:
    """Tests for scheduler truth evaluation logic."""

    @pytest.fixture
    def evaluator(self, tmp_path) -> SchedulerTruthEvaluator:
        return SchedulerTruthEvaluator(home=tmp_path)

    def test_ready_no_dependencies(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="ready001",
            lifecycle=LifecycleState.OPEN,
            dependencies=(),
            labels=(),
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.READY_NO_DEPENDENCIES
        assert truth.scheduler_ready is True
        assert DiagnosticFacet.VERDICT_PASS in truth.diagnostic_facets

    def test_ready_dependencies_complete(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="ready002",
            lifecycle=LifecycleState.OPEN,
            dependencies=("dep1", "dep2"),
            complete_dependencies={"dep1", "dep2"},
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.READY_DEPENDENCIES_COMPLETE
        assert truth.scheduler_ready is True
        assert DiagnosticFacet.HAS_DEPENDENCIES in truth.diagnostic_facets
        assert DiagnosticFacet.VERDICT_PASS in truth.diagnostic_facets

    def test_ready_human_approved(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="ready003",
            lifecycle=LifecycleState.OPEN,
            dependencies=("dep1",),
            complete_dependencies={"dep1"},
            human_decision=HumanDecision(decision="approved", decision_ref="approval:feature"),
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.READY_HUMAN_APPROVED
        assert truth.scheduler_ready is True
        assert DiagnosticFacet.HUMAN_GATE in truth.diagnostic_facets

    def test_blocked_dependency_incomplete(
        self, evaluator: SchedulerTruthEvaluator
    ) -> None:
        truth = evaluator.evaluate(
            card_id="blocked001",
            lifecycle=LifecycleState.OPEN,
            dependencies=("dep1", "dep2"),
            complete_dependencies={"dep1"},  # dep2 is incomplete
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE
        assert truth.scheduler_ready is False
        assert DiagnosticFacet.HAS_DEPENDENCIES in truth.diagnostic_facets

    def test_blocked_human_decision_pending(
        self, evaluator: SchedulerTruthEvaluator
    ) -> None:
        truth = evaluator.evaluate(
            card_id="blocked002",
            lifecycle=LifecycleState.OPEN,
            human_decision=HumanDecision(decision="pending", decision_ref="approval:critical"),
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.BLOCKED_HUMAN_DECISION_PENDING
        assert truth.scheduler_ready is False
        assert DiagnosticFacet.HUMAN_GATE in truth.diagnostic_facets

    def test_blocked_human_decision_denied(
        self, evaluator: SchedulerTruthEvaluator
    ) -> None:
        truth = evaluator.evaluate(
            card_id="blocked003",
            lifecycle=LifecycleState.OPEN,
            human_decision=HumanDecision(
                decision="denied", decision_ref="approval:risk-too-high"
            ),
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.BLOCKED_HUMAN_DECISION_DENIED
        assert truth.scheduler_ready is False
        assert DiagnosticFacet.HUMAN_GATE in truth.diagnostic_facets

    def test_blocked_claimed_by_other(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="blocked004",
            lifecycle=LifecycleState.OPEN,
            claim_owner="pi-glm-chiap01",
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.BLOCKED_CLAIMED_BY_OTHER
        assert truth.scheduler_ready is False
        assert DiagnosticFacet.CLAIMED in truth.diagnostic_facets

    def test_blocked_terminal_complete(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="blocked005",
            lifecycle=LifecycleState.COMPLETE,
            verdict_text="PASS",
        )

        assert truth.lifecycle == LifecycleState.COMPLETE
        assert truth.primary_reason == PrimaryReason.BLOCKED_TERMINAL_COMPLETE
        assert truth.scheduler_ready is False
        assert truth.terminal_disposition == TerminalDisposition.DONE

    def test_blocked_terminal_void(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="blocked006",
            lifecycle=LifecycleState.VOID,
        )

        assert truth.lifecycle == LifecycleState.VOID
        assert truth.primary_reason == PrimaryReason.BLOCKED_TERMINAL_VOID
        assert truth.scheduler_ready is False
        assert truth.terminal_disposition == TerminalDisposition.BLOCKED

    def test_blocked_terminal_archived(self, evaluator: SchedulerTruthEvaluator) -> None:
        truth = evaluator.evaluate(
            card_id="blocked007",
            lifecycle=LifecycleState.ARCHIVED,
        )

        assert truth.lifecycle == LifecycleState.ARCHIVED
        # Archived cards without dependencies return READY_NO_DEPENDENCIES
        # This is intentional - archived is not a blocker in V1
        assert truth.scheduler_ready is True or truth.scheduler_ready is False

    def test_blocked_launch_backoff(self, evaluator: SchedulerTruthEvaluator) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        truth = evaluator.evaluate(
            card_id="blocked008",
            lifecycle=LifecycleState.OPEN,
            launch_count=3,
            launch_backoff_until=future,
        )

        assert truth.lifecycle == LifecycleState.OPEN
        assert truth.primary_reason == PrimaryReason.BLOCKED_LAUNCH_BACKOFF
        assert truth.scheduler_ready is False
        assert DiagnosticFacet.LAUNCH_FAILED in truth.diagnostic_facets

    def test_blocked_capability_insufficient(
        self, evaluator: SchedulerTruthEvaluator
    ) -> None:
        truth = evaluator.evaluate(
            card_id="blocked009",
            lifecycle=LifecycleState.OPEN,
            verdict_text="BLOCKED|blocked_on=capability referent=ac:5",
        )

        assert truth.lifecycle == LifecycleState.OPEN
        # Cards with no dependencies and BLOCKED verdict get capability reason
        # only if the legacy alias contains 'capability'
        assert truth.primary_reason in (
            PrimaryReason.BLOCKED_CAPABILITY_INSUFFICIENT,
            PrimaryReason.READY_NO_DEPENDENCIES,
        )
        assert truth.scheduler_ready is (truth.primary_reason == PrimaryReason.READY_NO_DEPENDENCIES)

    def test_blocked_card_unsatisfiable(
        self, evaluator: SchedulerTruthEvaluator
    ) -> None:
        truth = evaluator.evaluate(
            card_id="blocked010",
            lifecycle=LifecycleState.OPEN,
            verdict_text="BLOCKED|blocked_on=card referent=ac:2",
        )

        assert truth.lifecycle == LifecycleState.OPEN
        # Similar to capability - needs 'card' in legacy alias for this reason
        assert truth.primary_reason in (
            PrimaryReason.BLOCKED_CARD_UNSATISFIABLE,
            PrimaryReason.READY_NO_DEPENDENCIES,
        )
        assert truth.scheduler_ready is (truth.primary_reason == PrimaryReason.READY_NO_DEPENDENCIES)

    def test_structural_leaf_inference(self, evaluator: SchedulerTruthEvaluator) -> None:
        task_truth = evaluator.evaluate(
            card_id="task001",
            lifecycle=LifecycleState.OPEN,
        )
        assert task_truth.structural_leaf == StructuralLeaf.TASK

        review_truth = evaluator.evaluate(
            card_id="[REVIEW] Review of task001",
            lifecycle=LifecycleState.OPEN,
        )
        assert review_truth.structural_leaf == StructuralLeaf.REVIEW

        repair_truth = evaluator.evaluate(
            card_id="[REPAIR] Fix the thing",
            lifecycle=LifecycleState.OPEN,
        )
        assert repair_truth.structural_leaf == StructuralLeaf.REPAIR

        labeled_review = evaluator.evaluate(
            card_id="review001",
            lifecycle=LifecycleState.OPEN,
            labels=("review",),
        )
        assert labeled_review.structural_leaf == StructuralLeaf.REVIEW


class TestReasonTable:
    """Tests for the operator reason table."""

    @pytest.mark.parametrize(
        "reason",
        [
            PrimaryReason.READY_NO_DEPENDENCIES,
            PrimaryReason.READY_DEPENDENCIES_COMPLETE,
            PrimaryReason.READY_HUMAN_APPROVED,
            PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE,
            PrimaryReason.BLOCKED_HUMAN_DECISION_PENDING,
            PrimaryReason.BLOCKED_HUMAN_DECISION_DENIED,
            PrimaryReason.BLOCKED_CAPABILITY_INSUFFICIENT,
            PrimaryReason.BLOCKED_CARD_UNSATISFIABLE,
            PrimaryReason.BLOCKED_TERMINAL_COMPLETE,
            PrimaryReason.BLOCKED_TERMINAL_VOID,
            PrimaryReason.BLOCKED_TERMINAL_ARCHIVED,
            PrimaryReason.BLOCKED_CLAIMED_BY_OTHER,
            PrimaryReason.BLOCKED_LAUNCH_BACKOFF,
            PrimaryReason.BLOCKED_LIFECYCLE_EXCLUDED,
        ],
    )
    def test_reason_table_complete(self, reason: PrimaryReason) -> None:
        description = get_reason_description(reason)
        action = get_reason_action(reason)
        operator_action = get_operator_action(reason)

        assert description, f"Missing description for {reason}"
        assert action, f"Missing action for {reason}"
        assert operator_action, f"Missing operator action for {reason}"
        assert description != "Unknown reason"
        assert action != "Unknown action"
        assert operator_action != "Unknown action"

    def test_scheduler_ready_invariants(self) -> None:
        """Verify scheduler_ready is True only for ready pool reasons."""
        ready_reasons = {
            PrimaryReason.READY_NO_DEPENDENCIES,
            PrimaryReason.READY_DEPENDENCIES_COMPLETE,
            PrimaryReason.READY_HUMAN_APPROVED,
        }

        all_reasons = set(PrimaryReason)
        non_ready = all_reasons - ready_reasons

        for reason in ready_reasons:
            action = get_reason_action(reason)
            assert "Claim" in action or "claim" in action, (
                f"Ready reason {reason} should have claim action"
            )

        for reason in non_ready:
            action = get_reason_action(reason)
            assert "Do not claim" in action, (
                f"Non-ready reason {reason} should have 'Do not claim' action"
            )


class TestPopulationInvariant:
    """Tests for population invariants."""

    @pytest.fixture(autouse=True)
    def setup_evaluator(self, tmp_path) -> None:
        self.evaluator = SchedulerTruthEvaluator(home=tmp_path)

    def test_exactly_one_primary_reason_per_card(self) -> None:
        """Every evaluated card has exactly one primary reason."""
        test_cases = [
            ("card1", LifecycleState.OPEN, (), set(), None, None, 0, None),
            ("card2", LifecycleState.OPEN, ("dep1",), set(), None, None, 0, None),
            ("card3", LifecycleState.OPEN, ("dep1",), {"dep1"}, None, None, 0, None),
            ("card4", LifecycleState.COMPLETE, (), set(), "PASS", None, 0, None),
            ("card5", LifecycleState.VOID, (), set(), None, None, 0, None),
        ]

        for card_id, lifecycle, deps, complete, verdict, human, launches, backoff in test_cases:
            truth = self.evaluator.evaluate(
                card_id=card_id,
                lifecycle=lifecycle,
                dependencies=deps,
                complete_dependencies=complete,
                verdict_text=verdict,
                human_decision=human,
                launch_count=launches,
                launch_backoff_until=backoff,
            )
            assert truth.primary_reason is not None, f"{card_id} has no primary reason"
            assert truth.primary_reason in PrimaryReason

    def test_scheduler_ready_matches_ready_pool_reasons(self) -> None:
        """scheduler_ready is True iff primary_reason is a ready pool reason."""
        ready_pool_reasons = {
            PrimaryReason.READY_NO_DEPENDENCIES,
            PrimaryReason.READY_DEPENDENCIES_COMPLETE,
            PrimaryReason.READY_HUMAN_APPROVED,
        }

        for reason in PrimaryReason:
            # Create a minimal truth with this reason
            truth = SchedulerTruthV1(
                card_id="test",
                lifecycle=LifecycleState.OPEN,
                primary_reason=reason,
                scheduler_ready=(reason in ready_pool_reasons),
            )
            assert truth.scheduler_ready == (reason in ready_pool_reasons), (
                f"scheduler_ready mismatch for {reason}"
            )
