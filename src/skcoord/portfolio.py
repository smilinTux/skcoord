"""Pure, fail-closed Portfolio Steward policy evaluation.

This module has no CardStore, authorization, model, network, CLI, or mutation
dependency. Callers supply one frozen synthetic or authorized snapshot and a
fixed ``as_of`` time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from skcoord.portfolio_contracts import (
    AgentCapacity,
    AllocationDecisionV1,
    CandidateExclusion,
    CanonicalAbstention,
    CanonicalSourceRef,
    CanonicalWarning,
    ClassOfService,
    CompletionDecision,
    EligibilityDecision,
    PlanDataQuality,
    PortfolioPlanContentV1,
    PortfolioPlanPresentationV1,
    PortfolioPlanProposalV1,
    PortfolioPolicy,
    RankedCandidate,
    ReviewAssignmentV1,
    ReviewDecisionV1,
    ServiceProfileV1,
    WorkCandidate,
    canonical_json_bytes,
    canonical_sha256,
)
from skcoord.portfolio_invocation import (
    ActingContextV1,
    DecisionContextV1,
    HumanContextV1,
    PresentationContextV1,
    RoleInvocationV1,
    RunContextV1,
    SanitizedAuthorizationV1,
    TargetContextV1,
)

__all__ = [
    "AgentCapacity",
    "AllocationDecisionV1",
    "ActingContextV1",
    "CandidateExclusion",
    "CanonicalAbstention",
    "CanonicalSourceRef",
    "CanonicalWarning",
    "CompletionDecision",
    "DecisionContextV1",
    "EligibilityDecision",
    "HumanContextV1",
    "PlanDataQuality",
    "PortfolioPlanContentV1",
    "PortfolioPlanPresentationV1",
    "PortfolioPlanProposalV1",
    "PortfolioPolicy",
    "PresentationContextV1",
    "RankedCandidate",
    "ReviewAssignmentV1",
    "ReviewDecisionV1",
    "RoleInvocationV1",
    "RunContextV1",
    "SanitizedAuthorizationV1",
    "ServiceProfileV1",
    "TargetContextV1",
    "WorkCandidate",
    "canonical_json_bytes",
    "canonical_sha256",
    "evaluate_portfolio",
    "evaluate_review_completion",
]


def _aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def _global_quality_reasons(
    quality: PlanDataQuality, policy: PortfolioPolicy, as_of: datetime
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if quality.parity_state != "healthy":
        reasons.add(f"parity_{quality.parity_state}")
    if quality.read_state != "healthy":
        reasons.add(f"board_read_{quality.read_state}")
    if not _aware(quality.observed_at) or not _aware(quality.expires_at):
        reasons.add("snapshot_timestamp_invalid")
        return tuple(sorted(reasons))
    if quality.observed_at > as_of:
        reasons.add("snapshot_from_future")
    if quality.expires_at <= as_of:
        reasons.add("snapshot_expired")
    if (as_of - quality.observed_at).total_seconds() > policy.snapshot_max_age_seconds:
        reasons.add("snapshot_stale")
    return tuple(sorted(reasons))


def _candidate_decision(
    card: WorkCandidate,
    capacity: AgentCapacity | None,
    policy: PortfolioPolicy,
    as_of: datetime,
) -> tuple[EligibilityDecision, CanonicalWarning | None]:
    reasons: set[str] = set()
    if card.kind != "task":
        reasons.add("not_task")
    if card.state not in policy.eligible_states:
        reasons.add("state_not_eligible")
    if "leaf-task" not in card.tags:
        reasons.add("not_leaf_task")
    if set(card.tags).intersection(policy.excluded_labels):
        reasons.add("excluded_label")
    if card.enrollment_state != "enrolled":
        reasons.add(f"enrollment_{card.enrollment_state}")
    if card.enrollment_policy_version != policy.enrollment_policy_version:
        reasons.add("enrollment_policy_mismatch")
    if not card.execution_ready_attestation:
        reasons.add("execution_ready_missing")
    if not card.acceptance_criteria:
        reasons.add("acceptance_missing")
    if len(card.repo_ids) != 1:
        reasons.add("repo_count_invalid")
    if len(card.size_values) != 1:
        reasons.add("size_count_invalid")
    elif card.size_values[0] not in {"S", "M", "L", "XL"}:
        reasons.add("size_invalid")

    dependency_ids = set(card.dependency_ids)
    if dependency_ids != set(card.dependency_states) or dependency_ids != set(
        card.dependency_revisions
    ):
        reasons.add("dependency_unknown")
    elif any(card.dependency_states[dep] != "done" for dep in card.dependency_ids):
        reasons.add("dependency_incomplete")

    if card.human_gate_state not in {"not-required", "approved"}:
        reasons.add(f"human_gate_{card.human_gate_state}")
    if card.human_gate_state == "approved" and (
        not card.approval_ref
        or not card.approved_card_hash
        or card.approved_card_revision != card.card_revision
    ):
        reasons.add("approval_version_mismatch")
    if card.owner_principal_id:
        reasons.add("already_owned")
    if card.lease_state != "clear":
        reasons.add(f"lease_{card.lease_state}")
    if card.lease_expires_at and not _aware(card.lease_expires_at):
        reasons.add("lease_timestamp_invalid")
    elif card.lease_expires_at and card.lease_expires_at > as_of:
        reasons.add("lease_active")
    if not _aware(card.ready_at):
        reasons.add("ready_time_missing" if card.ready_at is None else "ready_time_invalid")
    if card.class_of_service == "fixed-date" and not _aware(card.fixed_date_at):
        reasons.add(
            "fixed_date_missing" if card.fixed_date_at is None else "fixed_date_invalid"
        )

    normalized_class: ClassOfService = card.class_of_service
    warning: CanonicalWarning | None = None
    if card.class_of_service == "expedite" and (
        not card.expedite_approval_ref
        or not _aware(card.expedite_approval_expires_at)
        or card.expedite_approval_expires_at <= as_of
    ):
        normalized_class = "standard"
        warning = CanonicalWarning(code="expedite_downgraded", card_id=card.card_id)

    repo_id = card.repo_ids[0] if len(card.repo_ids) == 1 else None
    if capacity is None:
        reasons.add("capacity_unknown")
    else:
        if capacity.principal_id != card.target_executor_principal_id:
            reasons.add("executor_mismatch")
        if capacity.profile.profile_kind != "service":
            reasons.add("executor_profile_not_service")
        if capacity.profile.profile_state != "healthy":
            reasons.add(f"executor_profile_{capacity.profile.profile_state}")
        if not capacity.profile.memory_principal_id:
            reasons.add("executor_memory_unknown")
        if capacity.capability_state != "healthy":
            reasons.add(f"executor_capability_{capacity.capability_state}")
        if not _aware(capacity.observed_at) or not _aware(capacity.expires_at):
            reasons.add("capacity_timestamp_invalid")
        elif capacity.expires_at <= as_of or capacity.observed_at > as_of:
            reasons.add("capacity_stale")
        if not capacity.lease_state_fresh:
            reasons.add("capacity_lease_unknown")
        if capacity.active_wip != len(capacity.active_card_ids):
            reasons.add("capacity_conflict")
        if capacity.active_wip >= capacity.wip_limit:
            reasons.add("wip_exhausted")
        if card.kind not in capacity.allowed_task_classes:
            reasons.add("task_class_unauthorized")
        if repo_id is not None and repo_id not in capacity.allowed_repo_ids:
            reasons.add("repo_unauthorized")

    ranking_key: tuple[int | str, ...] | None = None
    if not reasons and card.ready_at is not None:
        fixed_date = (
            card.fixed_date_at.astimezone(timezone.utc).isoformat()
            if card.fixed_date_at
            else "9999-12-31T23:59:59+00:00"
        )
        ranking_key = (
            card.human_order if card.human_order is not None else 2**31 - 1,
            policy.class_order.index(normalized_class),
            -card.downstream_unlock_count,
            fixed_date,
            card.ready_at.astimezone(timezone.utc).isoformat(),
            policy.priority_order.index(card.priority),
            card.card_id,
        )
    return (
        EligibilityDecision(
            card_id=card.card_id,
            eligible=not reasons,
            reason_codes=tuple(sorted(reasons)),
            normalized_class=normalized_class,
            ranking_key=ranking_key,
        ),
        warning,
    )


def evaluate_portfolio(
    *,
    candidates: Sequence[WorkCandidate],
    capacities: Mapping[str, AgentCapacity],
    quality: PlanDataQuality,
    policy: PortfolioPolicy,
    objective_hash: str,
    as_of: datetime,
    source_refs: Sequence[CanonicalSourceRef] = (),
) -> PortfolioPlanContentV1:
    """Return a deterministic shadow proposal or explicit abstention."""
    if not _aware(as_of):
        raise ValueError("as_of must be timezone-aware")
    card_ids = [card.card_id for card in candidates]
    global_reasons = set(_global_quality_reasons(quality, policy, as_of))
    if len(set(card_ids)) != len(card_ids):
        global_reasons.add("duplicate_card_id")

    decisions: list[EligibilityDecision] = []
    warnings: list[CanonicalWarning] = []
    ordered_candidates = sorted(candidates, key=lambda item: item.card_id)
    if "duplicate_card_id" in global_reasons:
        ordered_candidates = list({card.card_id: card for card in ordered_candidates}.values())
    for card in ordered_candidates:
        if global_reasons:
            decision = EligibilityDecision(
                card_id=card.card_id,
                eligible=False,
                reason_codes=tuple(sorted(global_reasons)),
                normalized_class=card.class_of_service,
            )
            warning = None
        else:
            decision, warning = _candidate_decision(
                card, capacities.get(card.target_executor_principal_id), policy, as_of
            )
        decisions.append(decision)
        if warning:
            warnings.append(warning)

    eligible = sorted(
        (decision for decision in decisions if decision.eligible),
        key=lambda decision: decision.ranking_key or (),
    )
    cards_by_id = {card.card_id: card for card in candidates}
    ranked = tuple(
        RankedCandidate(
            card_id=decision.card_id,
            rank=index,
            repo_id=cards_by_id[decision.card_id].repo_ids[0],
            executor_principal_id=cards_by_id[
                decision.card_id
            ].target_executor_principal_id,
            class_of_service=decision.normalized_class,
            ranking_key=decision.ranking_key or (),
        )
        for index, decision in enumerate(eligible, start=1)
    )
    exclusions = tuple(
        CandidateExclusion(card_id=item.card_id, reason_codes=item.reason_codes)
        for item in decisions
        if not item.eligible
    )
    abstention_reasons = tuple(sorted(global_reasons))
    if not abstention_reasons and not ranked:
        abstention_reasons = ("no_eligible_candidates",)
    return PortfolioPlanContentV1(
        status="abstained" if abstention_reasons else "proposed",
        objective_hash=objective_hash,
        snapshot_id=quality.snapshot_id,
        snapshot_hash=quality.snapshot_hash,
        snapshot_expires_at=quality.expires_at,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        recommendations=ranked,
        exclusions=exclusions,
        warnings=tuple(sorted(warnings, key=lambda item: (item.card_id or "", item.code))),
        abstention=(
            CanonicalAbstention(reason_codes=abstention_reasons)
            if abstention_reasons
            else None
        ),
        source_refs=tuple(sorted(source_refs, key=lambda item: item.source_id)),
    )


def evaluate_review_completion(
    *,
    assignment: ReviewAssignmentV1,
    decision: ReviewDecisionV1 | None,
    artifact_revision: str,
    artifact_hash: str,
    as_of: datetime,
) -> CompletionDecision:
    """Evaluate exact review evidence and principal separation."""
    if not _aware(as_of):
        raise ValueError("as_of must be timezone-aware")
    reasons: set[str] = set()
    if assignment.artifact_revision != artifact_revision or assignment.artifact_hash != artifact_hash:
        reasons.add("assignment_artifact_mismatch")
    if not _aware(assignment.created_at) or not _aware(assignment.expires_at):
        reasons.add("assignment_timestamp_invalid")
    elif assignment.created_at > as_of or assignment.expires_at <= as_of:
        reasons.add("assignment_expired")
    denied = set(assignment.author_principal_ids) | set(
        assignment.disallowed_reviewer_principal_ids
    )
    if assignment.assigned_by_principal_id in denied:
        reasons.add("assignment_principal_conflict")
    if decision is None:
        reasons.add("review_missing")
    else:
        if decision.assignment_id != assignment.assignment_id:
            reasons.add("review_assignment_mismatch")
        if decision.artifact_hash != artifact_hash:
            reasons.add("review_artifact_mismatch")
        if decision.reviewer_capability != assignment.required_reviewer_capability:
            reasons.add("reviewer_capability_mismatch")
        if decision.verdict != "pass":
            reasons.add("review_failed")
        if not _aware(decision.decided_at) or not _aware(decision.expires_at):
            reasons.add("review_timestamp_invalid")
        elif (
            decision.decided_at < assignment.created_at
            or decision.decided_at > as_of
            or decision.expires_at <= as_of
        ):
            reasons.add("review_expired")
        if decision.reviewer_principal_id in denied:
            reasons.add("reviewer_principal_conflict")
        if not decision.evidence_refs or not decision.findings_hash:
            reasons.add("review_evidence_missing")
    return CompletionDecision(allowed=not reasons, reason_codes=tuple(sorted(reasons)))
