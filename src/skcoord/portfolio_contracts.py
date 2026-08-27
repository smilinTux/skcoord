"""Closed, canonical contracts for persona-neutral portfolio planning."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

TruthState = Literal[
    "healthy", "degraded", "unsafe", "unknown", "unavailable", "unauthorized"
]
Priority = Literal["critical", "high", "medium", "low"]
ClassOfService = Literal["expedite", "fixed-date", "standard", "intangible"]
RankingKey = tuple[int, int, int, str, str, int, str]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractModel(BaseModel):
    """Immutable, closed input used by a canonical contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_value(value: Any) -> Any:
    """Return the integer-only, NFC subset of JSON Canonicalization Scheme."""
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical timestamps must be timezone-aware")
        utc = value.astimezone(timezone.utc)
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise ValueError("canonical contract values forbid floats")
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical map keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("canonical map keys collide after NFC normalization")
            normalized[normalized_key] = _canonical_value(item)
        return {
            key: normalized[key]
            for key in sorted(normalized, key=lambda item: item.encode("utf-16-be"))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a contract value to deterministic UTF-8 JSON bytes."""
    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Hash canonical contract bytes with SHA-256."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_sorted_unique(values: tuple[str, ...], field_name: str) -> None:
    normalized = tuple(unicodedata.normalize("NFC", item) for item in values)
    if (
        normalized != values
        or tuple(sorted(values)) != values
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"{field_name} must be sorted and unique")


class PortfolioPolicy(ContractModel):
    """Versioned deterministic portfolio policy."""

    policy_id: str
    policy_version: str
    policy_hash: Sha256
    enrollment_policy_version: str
    snapshot_max_age_seconds: int = Field(gt=0)
    eligible_states: tuple[str, ...] = ("backlog", "ready")
    class_order: tuple[ClassOfService, ...] = (
        "expedite",
        "fixed-date",
        "standard",
        "intangible",
    )
    priority_order: tuple[Priority, ...] = ("critical", "high", "medium", "low")
    excluded_labels: tuple[str, ...] = (
        "do-not-claim",
        "human-gate",
        "parent-container",
        "review-only",
        "staged",
        "superseded",
    )

    @model_validator(mode="after")
    def validate_orders(self) -> "PortfolioPolicy":
        """Reject ambiguous or incomplete order definitions."""
        classes = {"expedite", "fixed-date", "standard", "intangible"}
        if len(self.class_order) != len(classes) or set(self.class_order) != classes:
            raise ValueError("class_order must contain every class exactly once")
        priorities = {"critical", "high", "medium", "low"}
        if (
            len(self.priority_order) != len(priorities)
            or set(self.priority_order) != priorities
        ):
            raise ValueError("priority_order must contain every priority exactly once")
        _require_sorted_unique(self.eligible_states, "eligible_states")
        _require_sorted_unique(self.excluded_labels, "excluded_labels")
        return self


class PlanDataQuality(ContractModel):
    """Frozen truth-state evidence for one portfolio snapshot."""

    source_owner: str
    snapshot_id: str
    snapshot_hash: Sha256
    board_revision: str
    projection_revision: str
    parity_state: TruthState
    read_state: TruthState
    observed_at: datetime
    expires_at: datetime


class ServiceProfileV1(ContractModel):
    """Reference to a human or nonselectable service profile."""

    profile_id: str
    profile_kind: Literal["human", "service"]
    profile_state: TruthState
    selectable: bool
    fallback_eligible: bool
    memory_principal_id: str | None
    default_tools: tuple[str, ...] = ()
    capability_policy_ref: str | None
    profile_revision: str
    profile_hash: Sha256

    @model_validator(mode="after")
    def services_have_no_interactive_authority(self) -> "ServiceProfileV1":
        """Prevent a service record from becoming a selectable tool-bearing persona."""
        if self.profile_kind == "service" and (
            self.selectable or self.fallback_eligible or self.default_tools
        ):
            raise ValueError(
                "service profiles are nonselectable and have zero default tools"
            )
        return self


class AgentCapacity(ContractModel):
    """Versioned executor capacity observation."""

    principal_id: str
    profile: ServiceProfileV1
    allowed_task_classes: tuple[str, ...]
    allowed_repo_ids: tuple[str, ...]
    wip_limit: int = Field(ge=0)
    active_wip: int = Field(ge=0)
    active_card_ids: tuple[str, ...] = ()
    lease_state_fresh: bool
    capability_state: TruthState
    capacity_revision: str
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def canonical_arrays(self) -> "AgentCapacity":
        for name in ("allowed_task_classes", "allowed_repo_ids", "active_card_ids"):
            _require_sorted_unique(getattr(self, name), name)
        return self


class WorkCandidate(ContractModel):
    """Canonical facts for one possible work allocation."""

    card_id: str
    title: str
    kind: str
    state: str
    card_revision: str
    priority: Priority
    class_of_service: ClassOfService
    human_order: int | None = Field(default=None, ge=0)
    enrollment_state: Literal["unenrolled", "enrolled", "suspended"]
    enrollment_policy_version: str | None
    tags: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    dependency_states: Mapping[str, str]
    dependency_revisions: Mapping[str, str]
    acceptance_criteria: tuple[str, ...]
    repo_ids: tuple[str, ...]
    size_values: tuple[str, ...]
    execution_ready_attestation: str | None
    owner_principal_id: str | None
    lease_state: Literal["clear", "active", "unknown", "unavailable", "unauthorized"]
    lease_generation: int = Field(ge=0)
    lease_expires_at: datetime | None
    human_gate_state: Literal[
        "not-required", "pending", "approved", "rejected", "unknown"
    ]
    approval_ref: str | None
    approved_card_revision: str | None
    approved_card_hash: Sha256 | None
    target_executor_principal_id: str
    downstream_unlock_count: int = Field(default=0, ge=0)
    ready_at: datetime | None
    fixed_date_at: datetime | None = None
    expedite_approval_ref: str | None = None
    expedite_approval_expires_at: datetime | None = None

    @field_validator("dependency_states", "dependency_revisions")
    @classmethod
    def freeze_maps(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("dependency_states", "dependency_revisions")
    def serialize_maps(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def canonical_arrays(self) -> "WorkCandidate":
        for name in ("tags", "dependency_ids", "repo_ids", "size_values"):
            _require_sorted_unique(getattr(self, name), name)
        return self


class EligibilityDecision(ContractModel):
    card_id: str
    eligible: bool
    reason_codes: tuple[str, ...]
    normalized_class: ClassOfService
    ranking_key: RankingKey | None = None


class RankedCandidate(ContractModel):
    card_id: str
    rank: int = Field(gt=0)
    repo_id: str
    executor_principal_id: str
    class_of_service: ClassOfService
    ranking_key: RankingKey


class CandidateExclusion(ContractModel):
    card_id: str
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def canonical_reasons(self) -> "CandidateExclusion":
        _require_sorted_unique(self.reason_codes, "reason_codes")
        return self


class CanonicalWarning(ContractModel):
    code: str
    card_id: str | None = None


class CanonicalAbstention(ContractModel):
    reason_codes: tuple[str, ...]

    @model_validator(mode="after")
    def canonical_reasons(self) -> "CanonicalAbstention":
        _require_sorted_unique(self.reason_codes, "reason_codes")
        return self


class CanonicalSourceRef(ContractModel):
    source_id: str
    source_revision: str
    source_hash: Sha256


class PortfolioPlanContentV1(ContractModel):
    """Persona-invariant, authority-bearing advisory plan content."""

    schema_version: Literal["portfolio-plan-content.v1"] = "portfolio-plan-content.v1"
    producer_role: Literal["portfolio-steward"] = "portfolio-steward"
    authority: Literal["advisory"] = "advisory"
    mode: Literal["shadow"] = "shadow"
    status: Literal["proposed", "abstained"]
    objective_hash: Sha256
    snapshot_id: str
    snapshot_hash: Sha256
    snapshot_expires_at: datetime
    policy_id: str
    policy_version: str
    policy_hash: Sha256
    recommendations: tuple[RankedCandidate, ...]
    exclusions: tuple[CandidateExclusion, ...]
    warnings: tuple[CanonicalWarning, ...] = ()
    abstention: CanonicalAbstention | None = None
    source_refs: tuple[CanonicalSourceRef, ...] = ()
    claims: tuple[str, ...] = ()
    mutations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def advisory_content_has_no_actions(self) -> "PortfolioPlanContentV1":
        """Keep proposal content structurally incapable of carrying actions."""
        if self.claims or self.mutations:
            raise ValueError("advisory plan content cannot contain claims or mutations")
        if (self.status == "abstained") != (self.abstention is not None):
            raise ValueError("abstained status and abstention evidence must agree")
        recommendation_keys = tuple(item.ranking_key for item in self.recommendations)
        recommendation_ids = tuple(item.card_id for item in self.recommendations)
        if recommendation_keys != tuple(sorted(recommendation_keys)):
            raise ValueError("recommendations must be in deterministic ranking order")
        if tuple(item.rank for item in self.recommendations) != tuple(
            range(1, len(self.recommendations) + 1)
        ) or len(set(recommendation_ids)) != len(recommendation_ids):
            raise ValueError("recommendation ranks and card ids must be unique")
        exclusion_ids = tuple(item.card_id for item in self.exclusions)
        _require_sorted_unique(exclusion_ids, "exclusion card ids")
        warning_keys = tuple((item.card_id or "", item.code) for item in self.warnings)
        if warning_keys != tuple(sorted(warning_keys)) or len(set(warning_keys)) != len(
            warning_keys
        ):
            raise ValueError("warnings must be sorted and unique")
        source_ids = tuple(item.source_id for item in self.source_refs)
        _require_sorted_unique(source_ids, "source refs")
        return self

    def content_hash(self) -> str:
        """Return the canonical authority-bearing content hash."""
        return canonical_sha256(self)


class PortfolioPlanPresentationV1(ContractModel):
    """Persona-specific display envelope with no authority."""

    schema_version: Literal["portfolio-plan-presentation.v1"] = (
        "portfolio-plan-presentation.v1"
    )
    proposal_instance_id: str
    plan_content_hash: Sha256
    requested_by_subject_id: str
    presenter_agent_id: str
    interaction_profile_id: str
    soul_revision: str | None
    session_id: str
    objective_text: str
    rendered_text: str
    created_at: datetime
    expires_at: datetime
    correlation_id: str

    def presentation_hash(self) -> str:
        """Return the independently canonicalized presentation hash."""
        return canonical_sha256(self)


class PortfolioPlanProposalV1(ContractModel):
    """Bind authority content to a separate presentation envelope."""

    content: PortfolioPlanContentV1
    plan_content_hash: Sha256
    presentation: PortfolioPlanPresentationV1
    presentation_hash: Sha256

    @model_validator(mode="after")
    def verify_hashes(self) -> "PortfolioPlanProposalV1":
        """Reject mismatched content or presentation bindings."""
        if self.plan_content_hash != self.content.content_hash():
            raise ValueError("plan_content_hash does not match content")
        if self.presentation.plan_content_hash != self.plan_content_hash:
            raise ValueError("presentation is bound to another plan")
        if self.presentation_hash != self.presentation.presentation_hash():
            raise ValueError("presentation_hash does not match presentation")
        return self


class AllocationDecisionV1(ContractModel):
    """Complete model-free preconditions for one proposed claim."""

    schema_version: Literal["portfolio-allocation.v1"] = "portfolio-allocation.v1"
    decision_id: Sha256
    plan_content_hash: Sha256
    operation: Literal["claim"] = "claim"
    card_id: str
    expected_card_revision: str
    dependency_revision_vector: Mapping[str, str]
    dependency_vector_hash: Sha256
    approval_ref: str | None
    approval_hash: Sha256 | None
    approved_card_revision: str | None
    policy_id: str
    policy_version: str
    policy_hash: Sha256
    capacity_revision: str
    expected_active_wip: int = Field(ge=0)
    wip_limit: int = Field(ge=0)
    expected_lease_generation: int = Field(ge=0)
    target_executor_principal_id: str
    target_repo_id: str
    target_repo_revision: str
    eligible: bool
    reason_codes: tuple[str, ...]
    ranking_key: RankingKey
    requested_lease_seconds: int = Field(gt=0)
    idempotency_key: str
    authorization_state: Literal["pending"] = "pending"
    decided_at: datetime
    expires_at: datetime

    @field_validator("dependency_revision_vector")
    @classmethod
    def freeze_dependency_vector(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("dependency_revision_vector")
    def serialize_dependency_vector(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @classmethod
    def create(cls, **values: Any) -> "AllocationDecisionV1":
        """Build and bind a decision after Pydantic has normalized its fields."""
        supplied = dict(values)
        supplied["decision_id"] = "pending"
        supplied["dependency_vector_hash"] = canonical_sha256(
            supplied["dependency_revision_vector"]
        )
        draft = cls.model_construct(**supplied)
        supplied["decision_id"] = draft.calculated_decision_id()
        return cls.model_validate(supplied)

    def calculated_decision_id(self) -> str:
        """Content-address every decision field except the identifier itself."""
        return canonical_sha256(self.model_dump(exclude={"decision_id"}, mode="python"))

    @model_validator(mode="after")
    def verify_bindings(self) -> "AllocationDecisionV1":
        """Reject incomplete dependency or decision content bindings."""
        if self.dependency_vector_hash != canonical_sha256(
            self.dependency_revision_vector
        ):
            raise ValueError("dependency_vector_hash does not match revision vector")
        if self.decision_id != self.calculated_decision_id():
            raise ValueError("decision_id does not match decision content")
        if self.expires_at <= self.decided_at:
            raise ValueError("allocation decision must expire after it is created")
        return self


class ReviewAssignmentV1(ContractModel):
    """Versioned independent-review assignment."""

    assignment_id: str
    artifact_type: str
    artifact_id: str
    artifact_revision: str
    artifact_hash: Sha256
    author_principal_ids: tuple[str, ...]
    disallowed_reviewer_principal_ids: tuple[str, ...]
    required_reviewer_capability: str
    review_policy_id: str
    review_policy_version: str
    assigned_by_principal_id: str
    created_at: datetime
    expires_at: datetime


class ReviewDecisionV1(ContractModel):
    """Review verdict bound to an exact assignment and artifact."""

    assignment_id: str
    artifact_hash: Sha256
    reviewer_principal_id: str
    reviewer_capability: str
    verdict: Literal["pass", "fail"]
    findings_hash: Sha256
    evidence_refs: tuple[str, ...]
    decided_at: datetime
    expires_at: datetime


class CompletionDecision(ContractModel):
    allowed: bool
    reason_codes: tuple[str, ...]
