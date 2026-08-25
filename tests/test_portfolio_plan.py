"""Adversarial coverage for the pure Portfolio Steward policy contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from skcoord.portfolio import (
    ActingContextV1,
    AgentCapacity,
    AllocationDecisionV1,
    DecisionContextV1,
    HumanContextV1,
    PlanDataQuality,
    PortfolioPlanPresentationV1,
    PortfolioPlanProposalV1,
    PortfolioPolicy,
    PresentationContextV1,
    ReviewAssignmentV1,
    ReviewDecisionV1,
    RoleInvocationV1,
    RunContextV1,
    SanitizedAuthorizationV1,
    ServiceProfileV1,
    TargetContextV1,
    WorkCandidate,
    canonical_json_bytes,
    canonical_sha256,
    evaluate_portfolio,
    evaluate_review_completion,
)

AS_OF = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _policy() -> PortfolioPolicy:
    return PortfolioPolicy(
        policy_id="portfolio-shadow",
        policy_version="1",
        policy_hash=SHA,
        enrollment_policy_version="enrollment-v1",
        snapshot_max_age_seconds=300,
    )


def _quality(**changes: object) -> PlanDataQuality:
    values: dict[str, object] = {
        "source_owner": "cardstore",
        "snapshot_id": "snapshot-1",
        "snapshot_hash": SHA,
        "board_revision": "board-r1",
        "projection_revision": "projection-r1",
        "parity_state": "healthy",
        "read_state": "healthy",
        "observed_at": AS_OF - timedelta(seconds=30),
        "expires_at": AS_OF + timedelta(minutes=4),
    }
    values.update(changes)
    return PlanDataQuality(**values)


def _profile(**changes: object) -> ServiceProfileV1:
    values: dict[str, object] = {
        "profile_id": "autocoder-profile",
        "profile_kind": "service",
        "profile_state": "healthy",
        "selectable": False,
        "fallback_eligible": False,
        "memory_principal_id": "autocoder-memory",
        "default_tools": (),
        "capability_policy_ref": "capauth:portfolio-v1",
        "profile_revision": "profile-r1",
        "profile_hash": SHA,
    }
    values.update(changes)
    return ServiceProfileV1(**values)


def _capacity(**changes: object) -> AgentCapacity:
    values: dict[str, object] = {
        "principal_id": "autocoder",
        "profile": _profile(),
        "allowed_task_classes": ("task",),
        "allowed_repo_ids": ("skcoord",),
        "wip_limit": 2,
        "active_wip": 0,
        "active_card_ids": (),
        "lease_state_fresh": True,
        "capability_state": "healthy",
        "capacity_revision": "capacity-r1",
        "observed_at": AS_OF - timedelta(seconds=10),
        "expires_at": AS_OF + timedelta(minutes=2),
    }
    values.update(changes)
    return AgentCapacity(**values)


def _card(card_id: str = "card-a", **changes: object) -> WorkCandidate:
    values: dict[str, object] = {
        "card_id": card_id,
        "title": "Implement one bounded leaf",
        "kind": "task",
        "state": "backlog",
        "card_revision": "card-r1",
        "priority": "medium",
        "class_of_service": "standard",
        "human_order": None,
        "enrollment_state": "enrolled",
        "enrollment_policy_version": "enrollment-v1",
        "tags": ("leaf-task", "repo:skcoord", "size:M"),
        "dependency_ids": (),
        "dependency_states": {},
        "dependency_revisions": {},
        "acceptance_criteria": ("exact test passes",),
        "repo_ids": ("skcoord",),
        "size_values": ("M",),
        "execution_ready_attestation": "ready-r1",
        "owner_principal_id": None,
        "lease_state": "clear",
        "lease_generation": 0,
        "lease_expires_at": None,
        "human_gate_state": "not-required",
        "approval_ref": None,
        "approved_card_revision": None,
        "approved_card_hash": None,
        "target_executor_principal_id": "autocoder",
        "downstream_unlock_count": 0,
        "ready_at": AS_OF - timedelta(hours=1),
        "fixed_date_at": None,
        "expedite_approval_ref": None,
        "expedite_approval_expires_at": None,
    }
    values.update(changes)
    return WorkCandidate(**values)


def _plan(
    cards: tuple[WorkCandidate, ...],
    *,
    quality: PlanDataQuality | None = None,
    capacity: AgentCapacity | None = None,
):
    selected_capacity = capacity or _capacity()
    return evaluate_portfolio(
        candidates=cards,
        capacities={selected_capacity.principal_id: selected_capacity},
        quality=quality or _quality(),
        policy=_policy(),
        objective_hash=SHA,
        as_of=AS_OF,
    )


def _presentation(content_hash: str, persona: str) -> PortfolioPlanPresentationV1:
    return PortfolioPlanPresentationV1(
        proposal_instance_id=f"proposal-{persona}",
        plan_content_hash=content_hash,
        requested_by_subject_id="human-owner",
        presenter_agent_id=persona,
        interaction_profile_id=f"interaction-{persona}",
        soul_revision=f"soul-{persona}",
        session_id=f"session-{persona}",
        objective_text="Choose the next card",
        rendered_text=f"{persona} recommends card-a",
        created_at=AS_OF,
        expires_at=AS_OF + timedelta(minutes=2),
        correlation_id=f"correlation-{persona}",
    )


def test_persona_changes_only_the_presentation_envelope() -> None:
    content = _plan((_card(),))
    content_hash = content.content_hash()
    jarvis = _presentation(content_hash, "jarvis")
    ada = _presentation(content_hash, "ada")

    jarvis_proposal = PortfolioPlanProposalV1(
        content=content,
        plan_content_hash=content_hash,
        presentation=jarvis,
        presentation_hash=jarvis.presentation_hash(),
    )
    ada_proposal = PortfolioPlanProposalV1(
        content=content,
        plan_content_hash=content_hash,
        presentation=ada,
        presentation_hash=ada.presentation_hash(),
    )

    assert jarvis_proposal.plan_content_hash == ada_proposal.plan_content_hash
    assert jarvis_proposal.presentation_hash != ada_proposal.presentation_hash
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PortfolioPlanPresentationV1(
            **jarvis.model_dump(), acting_principal_id="portfolio-steward"
        )


def test_role_invocation_keeps_every_principal_and_scope_distinct() -> None:
    invocation = RoleInvocationV1(
        presentation=PresentationContextV1(
            presenter_agent_id="jarvis",
            interaction_profile_id="jarvis-default",
            soul_revision="soul-r1",
            session_id="presentation-session",
        ),
        human=HumanContextV1(
            subject_principal_id="human-owner",
            verified_session_id="verified-session",
        ),
        acting=ActingContextV1(
            principal_id="portfolio-steward",
            role_id="portfolio-steward",
            role_revision="role-r1",
            role_spec_hash=SHA,
        ),
        decision=DecisionContextV1(
            principal_id="portfolio-allocator",
            role_id="portfolio-allocator",
            role_revision="role-r1",
            role_spec_hash=SHA,
        ),
        target=TargetContextV1(snapshot_hash=SHA, card_ids=("card-a",)),
        authorization=SanitizedAuthorizationV1(
            decision_id="capauth-decision-1",
            state="authorized",
            capability_scope=("portfolio.propose", "portfolio.read"),
            policy_ref="capauth:portfolio-v1",
        ),
        run=RunContextV1(
            mode="propose",
            correlation_id="correlation-1",
            idempotency_key="proposal-1",
            route_id="local-qwen",
            route_revision="route-r1",
            prompt_hash=SHA,
            schema_hash=SHA,
        ),
    )

    assert invocation.presentation.presenter_agent_id == "jarvis"
    assert invocation.human.subject_principal_id == "human-owner"
    assert invocation.acting.principal_id == "portfolio-steward"
    assert invocation.decision.principal_id == "portfolio-allocator"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SanitizedAuthorizationV1(
            **invocation.authorization.model_dump(), raw_capability_token="secret"
        )


def test_canonical_hash_normalizes_nfc_and_rejects_ambiguous_values() -> None:
    assert canonical_sha256({"label": "e\u0301"}) == canonical_sha256(
        {"label": "\u00e9"}
    )
    utf16_order = canonical_json_bytes({"\ue000": 1, "\U00010000": 2}).decode()
    assert utf16_order.index("\U00010000") < utf16_order.index("\ue000")
    with pytest.raises(ValueError, match="forbid floats"):
        canonical_sha256({"score": 1.0})
    with pytest.raises(ValueError, match="collide"):
        canonical_sha256({"e\u0301": 1, "\u00e9": 2})

    card = _card(
        dependency_ids=("dep",),
        dependency_states={"dep": "done"},
        dependency_revisions={"dep": "r1"},
    )
    with pytest.raises(TypeError):
        card.dependency_states["dep"] = "doing"  # type: ignore[index]


def test_ranking_and_content_hash_are_input_order_invariant() -> None:
    later = _card("card-b", ready_at=AS_OF - timedelta(minutes=10))
    human_first = _card("card-a", human_order=1, ready_at=AS_OF)
    first = _plan((later, human_first))
    second = _plan((human_first, later))

    assert [item.card_id for item in first.recommendations] == ["card-a", "card-b"]
    assert first.content_hash() == second.content_hash()
    assert first.claims == ()
    assert first.mutations == ()


def test_unhealthy_snapshot_abstains_with_distinct_truth_states() -> None:
    plan = _plan(
        (_card(),),
        quality=_quality(parity_state="unsafe", read_state="unauthorized"),
    )

    assert plan.status == "abstained"
    assert plan.recommendations == ()
    assert plan.abstention is not None
    assert plan.abstention.reason_codes == ("board_read_unauthorized", "parity_unsafe")
    assert plan.exclusions[0].reason_codes == plan.abstention.reason_codes

    stale = _plan((_card(),), quality=_quality(expires_at=AS_OF))
    assert stale.status == "abstained"
    assert stale.abstention is not None
    assert "snapshot_expired" in stale.abstention.reason_codes


def test_conflicting_duplicate_card_ids_abstain_without_guessing() -> None:
    plan = _plan((_card(), _card()))

    assert plan.status == "abstained"
    assert plan.abstention is not None
    assert plan.abstention.reason_codes == ("duplicate_card_id",)
    assert plan.recommendations == ()


@pytest.mark.parametrize(
    ("card_changes", "capacity_changes", "expected_reason"),
    [
        ({"execution_ready_attestation": None}, {}, "execution_ready_missing"),
        ({"enrollment_state": "unenrolled"}, {}, "enrollment_unenrolled"),
        (
            {
                "dependency_ids": ("dep",),
                "dependency_states": {},
                "dependency_revisions": {},
            },
            {},
            "dependency_unknown",
        ),
        (
            {
                "dependency_ids": ("dep",),
                "dependency_states": {"dep": "doing"},
                "dependency_revisions": {"dep": "r1"},
            },
            {},
            "dependency_incomplete",
        ),
        ({"human_gate_state": "pending"}, {}, "human_gate_pending"),
        ({"state": "blocked"}, {}, "state_not_eligible"),
        ({"repo_ids": ("skcapstone", "skcoord")}, {}, "repo_count_invalid"),
        ({"size_values": ("L", "M")}, {}, "size_count_invalid"),
        ({"size_values": ("NOT-A-RECOGNIZED-SIZE",)}, {}, "size_invalid"),
        ({"owner_principal_id": "someone"}, {}, "already_owned"),
        ({"lease_state": "unknown"}, {}, "lease_unknown"),
        (
            {"lease_expires_at": AS_OF + timedelta(minutes=1)},
            {},
            "lease_active",
        ),
        ({"ready_at": None}, {}, "ready_time_missing"),
        ({}, {"active_wip": 2, "active_card_ids": ("card-1", "card-2")}, "wip_exhausted"),
        ({}, {"active_card_ids": ("already-active",)}, "capacity_conflict"),
        ({}, {"expires_at": AS_OF}, "capacity_stale"),
        ({}, {"capability_state": "unauthorized"}, "executor_capability_unauthorized"),
    ],
)
def test_hard_filters_exclude_ineligible_work(
    card_changes: dict[str, object],
    capacity_changes: dict[str, object],
    expected_reason: str,
) -> None:
    plan = _plan((_card(**card_changes),), capacity=_capacity(**capacity_changes))

    assert plan.status == "abstained"
    assert plan.recommendations == ()
    assert expected_reason in plan.exclusions[0].reason_codes


def test_expired_expedite_is_visibly_downgraded() -> None:
    plan = _plan((_card(class_of_service="expedite"),))

    assert plan.status == "proposed"
    assert plan.recommendations[0].class_of_service == "standard"
    assert plan.warnings[0].code == "expedite_downgraded"


def test_service_profile_cannot_be_selectable_or_tool_bearing() -> None:
    with pytest.raises(ValidationError, match="zero default tools"):
        _profile(default_tools=("shell",))
    with pytest.raises(ValidationError, match="nonselectable"):
        _profile(selectable=True)


def test_allocation_decision_binds_every_revision_and_identifier() -> None:
    values = {
        "plan_content_hash": SHA,
        "card_id": "card-a",
        "expected_card_revision": "card-r1",
        "dependency_revision_vector": {"dep": "dep-r1"},
        "approval_ref": "approval-1",
        "approval_hash": SHA,
        "approved_card_revision": "card-r1",
        "policy_id": "portfolio-shadow",
        "policy_version": "1",
        "policy_hash": SHA,
        "capacity_revision": "capacity-r1",
        "expected_active_wip": 0,
        "wip_limit": 1,
        "expected_lease_generation": 3,
        "target_executor_principal_id": "autocoder",
        "target_repo_id": "skcoord",
        "target_repo_revision": "repo-r1",
        "eligible": True,
        "reason_codes": (),
        "ranking_key": (1, 2, -3, "date", "ready", 2, "card-a"),
        "requested_lease_seconds": 300,
        "idempotency_key": "claim-card-a-r1",
        "decided_at": AS_OF,
        "expires_at": AS_OF + timedelta(minutes=1),
    }
    decision = AllocationDecisionV1.create(**values)

    assert decision.decision_id == decision.calculated_decision_id()
    assert decision.authorization_state == "pending"
    tampered = decision.model_dump()
    tampered["expected_card_revision"] = "card-r2"
    with pytest.raises(ValidationError, match="decision_id"):
        AllocationDecisionV1.model_validate(tampered)
    tampered = decision.model_dump()
    tampered["dependency_revision_vector"] = {"dep": "dep-r2"}
    with pytest.raises(ValidationError, match="dependency_vector_hash"):
        AllocationDecisionV1.model_validate(tampered)


def _assignment() -> ReviewAssignmentV1:
    return ReviewAssignmentV1(
        assignment_id="review-1",
        artifact_type="portfolio-plan",
        artifact_id="plan-1",
        artifact_revision="plan-r1",
        artifact_hash=SHA,
        author_principal_ids=("author",),
        disallowed_reviewer_principal_ids=("executor",),
        required_reviewer_capability="portfolio.review",
        review_policy_id="review-policy",
        review_policy_version="1",
        assigned_by_principal_id="scheduler",
        created_at=AS_OF - timedelta(minutes=2),
        expires_at=AS_OF + timedelta(minutes=2),
    )


def _review(**changes: object) -> ReviewDecisionV1:
    values: dict[str, object] = {
        "assignment_id": "review-1",
        "artifact_hash": SHA,
        "reviewer_principal_id": "reviewer",
        "reviewer_capability": "portfolio.review",
        "verdict": "pass",
        "findings_hash": SHA,
        "evidence_refs": ("evidence-1",),
        "decided_at": AS_OF - timedelta(minutes=1),
        "expires_at": AS_OF + timedelta(minutes=1),
    }
    values.update(changes)
    return ReviewDecisionV1(**values)


def test_review_completion_requires_independence_and_exact_pass_evidence() -> None:
    assignment = _assignment()
    allowed = evaluate_review_completion(
        assignment=assignment,
        decision=_review(),
        artifact_revision="plan-r1",
        artifact_hash=SHA,
        as_of=AS_OF,
    )
    denied = evaluate_review_completion(
        assignment=assignment,
        decision=_review(reviewer_principal_id="author"),
        artifact_revision="plan-r1",
        artifact_hash=SHA,
        as_of=AS_OF,
    )
    missing = evaluate_review_completion(
        assignment=assignment,
        decision=None,
        artifact_revision="plan-r1",
        artifact_hash=SHA,
        as_of=AS_OF,
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    assert "reviewer_principal_conflict" in denied.reason_codes
    assert missing.allowed is False
    assert missing.reason_codes == ("review_missing",)
