"""Typed trusted-role invocation attribution for portfolio planning."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from skcoord.portfolio_contracts import ContractModel, Sha256, _require_sorted_unique


class PresentationContextV1(ContractModel):
    """Human-selected interaction context with no authority fields."""

    presenter_agent_id: str
    interaction_profile_id: str
    soul_revision: str | None
    session_id: str


class HumanContextV1(ContractModel):
    """Server-verified requesting human."""

    subject_principal_id: str
    verified_session_id: str


class ActingContextV1(ContractModel):
    """Server-derived advisory service identity."""

    principal_id: str
    role_id: str
    role_revision: str
    role_spec_hash: Sha256


class DecisionContextV1(ContractModel):
    """Server-derived deterministic decision service identity."""

    principal_id: str
    role_id: str
    role_revision: str
    role_spec_hash: Sha256


class TargetContextV1(ContractModel):
    """Exact immutable board target."""

    snapshot_hash: Sha256
    card_ids: tuple[str, ...]

    @model_validator(mode="after")
    def canonical_cards(self) -> "TargetContextV1":
        _require_sorted_unique(self.card_ids, "target card ids")
        return self


class SanitizedAuthorizationV1(ContractModel):
    """Sanitized decision metadata without a capability token."""

    decision_id: str
    state: Literal["authorized", "denied", "unknown", "unavailable", "unauthorized"]
    capability_scope: tuple[str, ...]
    policy_ref: str

    @model_validator(mode="after")
    def canonical_scope(self) -> "SanitizedAuthorizationV1":
        _require_sorted_unique(self.capability_scope, "capability scope")
        return self


class RunContextV1(ContractModel):
    """Bounded read or proposal run metadata."""

    mode: Literal["analyze", "propose"]
    correlation_id: str
    idempotency_key: str
    route_id: str
    route_revision: str
    prompt_hash: Sha256
    schema_hash: Sha256


class RoleInvocationV1(ContractModel):
    """Keep human, presenter, acting, and decision principals explicit."""

    presentation: PresentationContextV1
    human: HumanContextV1
    acting: ActingContextV1
    decision: DecisionContextV1
    target: TargetContextV1
    authorization: SanitizedAuthorizationV1
    run: RunContextV1
