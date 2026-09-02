"""Versioned, read-only scheduler truth over folded cards.

Lifecycle, outcome, human authority, and eligibility are deliberately separate.
A DONE card is not inferred to have passed review.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from enum import Enum
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .card import Card, Column, Kind


class SchedulerOutcome(str, Enum):
    PASS_FOR_REVIEW = "pass_for_review"
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    WORKER_DIED = "worker_died"


_LEGACY_OUTCOME_RE = re.compile(
    r"^\s*(PASS(?:_FOR_[A-Z_]+)?|BLOCKED(?:_[A-Z_]+)?|FAIL(?:_[A-Z_]+)?|WORKER_DIED)"
    r"(?:\b|(?=[|.:]))",
    re.IGNORECASE,
)
_KNOWN_PROVISIONAL_PASS_ALIASES = frozenset(
    {
        "PASS_FOR_REVIEW",
        "PASS_FOR_INDEPENDENT_REVIEW",
        "PASS_FOR_REREVIEW",
        "PASS_FOR_INDEPENDENT_REREVIEW",
        "PASS_FOR_INTEGRATION",
        "PASS_FOR_QUALIFICATION",
        "PASS_FOR_ASSEMBLY",
        "PASS_FOR_REVIEW_R",
        "PASS_FOR_REVIEW_PACKET_ONLY_EXECUTION_UNAUTHORIZED",
        "PASS_FOR_INDEPENDENT_REVIEW_ONLY_",
    }
)


def normalize_scheduler_outcome(value: str) -> SchedulerOutcome | None:
    """Fold explicit legacy verdict prefixes without inferring from prose."""
    match = _LEGACY_OUTCOME_RE.match(value)
    if not match:
        return None
    token = match.group(1).upper()
    if token.startswith("PASS_FOR_"):
        return (
            SchedulerOutcome.PASS_FOR_REVIEW
            if token in _KNOWN_PROVISIONAL_PASS_ALIASES
            else None
        )
    if token.startswith("BLOCKED"):
        return SchedulerOutcome.BLOCKED
    if token.startswith("FAIL"):
        return SchedulerOutcome.FAIL
    return SchedulerOutcome[token]


class BlockerCategory(str, Enum):
    DEPENDENCY = "dependency"
    CARD = "card"
    HUMAN = "human"
    CAPABILITY = "capability"


class HumanDecision(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    HOLD = "hold"
    VOID = "void"
    NO_ACTION = "no_action"
    ATTESTED = "attested"
    UNKNOWN = "unknown"


class EligibilityReason(str, Enum):
    READY = "ready"
    MALFORMED = "malformed"
    NON_TASK = "non_task"
    TERMINAL_DONE = "terminal_done"
    ARCHIVED = "archived"
    OWNED = "owned"
    STATE_NOT_ELIGIBLE = "state_not_eligible"
    DEPENDENCY_UNKNOWN = "dependency_unknown"
    DEPENDENCY_INCOMPLETE = "dependency_incomplete"
    HUMAN_GATE_PENDING = "human_gate_pending"
    EXPLICIT_CLAIM_DENIAL = "explicit_claim_denial"
    SUPERSEDED = "superseded"
    CONTAINER = "container"
    FOREIGN_PROJECT = "foreign_project"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED_UNCHANGED = "blocked_unchanged"
    HOST_PINNED_ELSEWHERE = "host_pinned_elsewhere"
    SENSITIVE_UNAPPROVED = "sensitive_unapproved"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"


class WorkClass(str, Enum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    EXCLUDED = "excluded"


class SchedulerBlockerV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: BlockerCategory
    referents: tuple[str, ...] = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: str | None = None

    @model_validator(mode="after")
    def canonical_referents(self) -> "SchedulerBlockerV1":
        if self.referents != tuple(sorted(set(self.referents))):
            raise ValueError("referents must be sorted and unique")
        return self


class SchedulerTruthV1(BaseModel):
    """One structural scheduling decision with optional typed evidence facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["scheduler-truth.v1"] = "scheduler-truth.v1"
    card_id: str = Field(min_length=1)
    lifecycle: Column
    terminal: bool
    work_class: WorkClass
    structural_leaf: bool
    structural_eligible: bool
    scheduler_ready: bool | None = None
    structural_reason: EligibilityReason
    primary_reason: EligibilityReason | None = None
    reason_codes: tuple[EligibilityReason, ...]
    diagnostic_facets: tuple[EligibilityReason, ...] = ()
    outcome: SchedulerOutcome | None = None
    blocker: SchedulerBlockerV1 | None = None
    human_decision: HumanDecision = HumanDecision.UNKNOWN

    @model_validator(mode="after")
    def coherent(self) -> "SchedulerTruthV1":
        if self.reason_codes != (self.structural_reason, *self.diagnostic_facets):
            raise ValueError(
                "reason_codes must contain structural reason followed by facets"
            )
        if self.diagnostic_facets != tuple(dict.fromkeys(self.diagnostic_facets)):
            raise ValueError("diagnostic_facets must be unique")
        if self.structural_reason in self.diagnostic_facets:
            raise ValueError("structural_reason cannot also be a diagnostic facet")
        if self.structural_eligible != (self.work_class is not WorkClass.EXCLUDED):
            raise ValueError("work_class and structural_eligible disagree")
        if self.structural_leaf != self.structural_eligible:
            raise ValueError("structural_leaf and structural_eligible disagree")
        if self.structural_eligible != (
            self.structural_reason is EligibilityReason.READY
        ):
            raise ValueError("structural reason and structural eligibility disagree")
        if self.scheduler_ready is None:
            if self.primary_reason is not None:
                raise ValueError(
                    "uncomposed runtime readiness cannot have a primary reason"
                )
        elif self.scheduler_ready:
            if (
                not self.structural_eligible
                or self.primary_reason is not EligibilityReason.READY
            ):
                raise ValueError(
                    "runtime ready requires structural eligibility and ready reason"
                )
        elif self.primary_reason in {None, EligibilityReason.READY}:
            raise ValueError("runtime exclusion requires a non-ready primary reason")
        elif (
            not self.structural_eligible
            and self.primary_reason is not self.structural_reason
        ):
            raise ValueError(
                "structural exclusion must remain the final primary reason"
            )
        if self.outcome is SchedulerOutcome.BLOCKED and self.blocker is None:
            raise ValueError("a blocked outcome requires typed blocker evidence")
        if self.blocker is not None and self.outcome is not SchedulerOutcome.BLOCKED:
            raise ValueError("blocker evidence requires a blocked outcome")
        return self


class SchedulerTruthSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["scheduler-truth-snapshot.v1"] = (
        "scheduler-truth-snapshot.v1"
    )
    cards: tuple[SchedulerTruthV1, ...]
    population: int = Field(ge=0)
    ready: int = Field(ge=0)
    exclusive_counts: dict[EligibilityReason, int]
    overlap_counts: dict[EligibilityReason, int]
    implementation: int = Field(ge=0)
    review: int = Field(ge=0)
    excluded: int = Field(ge=0)
    malformed: int = Field(ge=0)

    @model_validator(mode="after")
    def complete_partition(self) -> "SchedulerTruthSnapshotV1":
        if self.population != len(self.cards):
            raise ValueError("population must equal the number of cards")
        if self.ready != sum(card.structural_eligible for card in self.cards):
            raise ValueError("ready must equal structurally eligible cards")
        exclusive = Counter(card.structural_reason for card in self.cards)
        expected_exclusive = dict(
            sorted(exclusive.items(), key=lambda row: row[0].value)
        )
        if self.exclusive_counts != expected_exclusive:
            raise ValueError("exclusive counts do not match structural reasons")
        if sum(self.exclusive_counts.values()) != self.population:
            raise ValueError("exclusive counts must partition the population")
        overlap = Counter(reason for card in self.cards for reason in card.reason_codes)
        expected_overlap = dict(sorted(overlap.items(), key=lambda row: row[0].value))
        if self.overlap_counts != expected_overlap:
            raise ValueError("overlap counts do not match card reason codes")
        if self.ready + self.excluded != self.population:
            raise ValueError("ready and excluded must partition the population")
        if self.implementation + self.review != self.ready:
            raise ValueError("implementation and review must partition ready")
        if self.excluded != sum(
            card.work_class is WorkClass.EXCLUDED for card in self.cards
        ):
            raise ValueError("excluded must match excluded cards")
        if self.malformed != expected_exclusive.get(EligibilityReason.MALFORMED, 0):
            raise ValueError("malformed must match malformed structural reasons")
        return self


_LABEL_ALIASES = {
    "not-claimable": "do-not-claim",
    "sprint-container": "parent-container",
}
_CLAIM_DENIAL_LABELS = frozenset({"do-not-claim"})
_CONTAINER_LABELS = frozenset({"parent-container"})
_PRECEDENCE = (
    EligibilityReason.MALFORMED,
    EligibilityReason.TERMINAL_DONE,
    EligibilityReason.ARCHIVED,
    EligibilityReason.SUPERSEDED,
    EligibilityReason.NON_TASK,
    EligibilityReason.OWNED,
    EligibilityReason.STATE_NOT_ELIGIBLE,
    EligibilityReason.CONTAINER,
    EligibilityReason.EXPLICIT_CLAIM_DENIAL,
    EligibilityReason.HUMAN_GATE_PENDING,
    EligibilityReason.FOREIGN_PROJECT,
    EligibilityReason.DEPENDENCY_UNKNOWN,
    EligibilityReason.DEPENDENCY_INCOMPLETE,
)


def canonical_label(label: str) -> str:
    """Fold one legacy enforcing-label alias for reads and future writes."""
    normalized = label.strip().lower().replace("_", "-")
    return _LABEL_ALIASES.get(normalized, normalized)


def canonical_labels_for_write(labels: Sequence[str]) -> tuple[str, ...]:
    """Return canonical new labels without touching historical events."""
    return tuple(sorted({canonical_label(label) for label in labels if label.strip()}))


def _reasons(
    card: Card, cards_by_id: dict[str, Card], parent_ids: set[str]
) -> tuple[EligibilityReason, ...]:
    labels = {canonical_label(label) for label in card.labels}
    found: set[EligibilityReason] = set()
    if card.kind not in {Kind.TASK, Kind.EPIC}:
        found.add(EligibilityReason.NON_TASK)
    if card.status is Column.DONE:
        found.add(EligibilityReason.TERMINAL_DONE)
    if card.archived:
        found.add(EligibilityReason.ARCHIVED)
    if card.owner:
        found.add(EligibilityReason.OWNED)
    if card.status not in {Column.BACKLOG, Column.REVIEW}:
        found.add(EligibilityReason.STATE_NOT_ELIGIBLE)
    if (
        card.kind is Kind.EPIC
        or card.id in parent_ids
        or labels & _CONTAINER_LABELS
        or "[epic]" in card.title.lower()
        or "[sprint " in card.title.lower()
    ):
        found.add(EligibilityReason.CONTAINER)
    if "superseded" in labels or any(
        label.startswith("superseded-") for label in labels
    ):
        found.add(EligibilityReason.SUPERSEDED)
    if labels & _CLAIM_DENIAL_LABELS or any(
        "do-not-claim" in label for label in labels
    ):
        found.add(EligibilityReason.EXPLICIT_CLAIM_DENIAL)
    if "human-gate" in labels or "[human]" in card.title.lower():
        found.add(EligibilityReason.HUMAN_GATE_PENDING)
    if "foreign-project" in labels:
        found.add(EligibilityReason.FOREIGN_PROJECT)
    for dependency in card.dependencies:
        target = cards_by_id.get(dependency)
        if target is None:
            found.add(EligibilityReason.DEPENDENCY_UNKNOWN)
        elif target.status is not Column.DONE:
            found.add(EligibilityReason.DEPENDENCY_INCOMPLETE)
    if not card.id.strip() or card.title.strip().lower() in {"", "x"}:
        found.add(EligibilityReason.MALFORMED)
    return tuple(reason for reason in _PRECEDENCE if reason in found)


def classify_structural_cards(cards: Sequence[Card]) -> SchedulerTruthSnapshotV1:
    """Classify folded cards without inferring runtime readiness or outcomes."""
    cards_by_id = {card.id: card for card in cards}
    parent_ids = {
        canonical_label(label).removeprefix("parent-")
        for card in cards
        for label in card.labels
        if canonical_label(label).startswith("parent-")
        and len(canonical_label(label)) > len("parent-")
    }
    decisions: list[SchedulerTruthV1] = []
    for card in sorted(cards, key=lambda item: item.id):
        exclusions = _reasons(card, cards_by_id, parent_ids)
        if exclusions:
            work_class = WorkClass.EXCLUDED
            primary = exclusions[0]
            facets = exclusions[1:]
        else:
            work_class = (
                WorkClass.REVIEW
                if card.status is Column.REVIEW
                else WorkClass.IMPLEMENTATION
            )
            primary = EligibilityReason.READY
            facets = (
                (EligibilityReason.AWAITING_REVIEW,)
                if work_class is WorkClass.REVIEW
                else ()
            )
        eligible = work_class is not WorkClass.EXCLUDED
        decisions.append(
            SchedulerTruthV1(
                card_id=card.id,
                lifecycle=card.status,
                terminal=card.status is Column.DONE or card.archived,
                work_class=work_class,
                structural_leaf=eligible,
                structural_eligible=eligible,
                structural_reason=primary,
                reason_codes=(primary, *facets),
                diagnostic_facets=facets,
            )
        )
    exclusive = Counter(item.structural_reason for item in decisions)
    overlap = Counter(reason for item in decisions for reason in item.reason_codes)
    ready = sum(item.structural_eligible for item in decisions)
    return SchedulerTruthSnapshotV1(
        cards=tuple(decisions),
        population=len(decisions),
        ready=ready,
        exclusive_counts=dict(sorted(exclusive.items(), key=lambda row: row[0].value)),
        overlap_counts=dict(sorted(overlap.items(), key=lambda row: row[0].value)),
        implementation=sum(
            item.work_class is WorkClass.IMPLEMENTATION for item in decisions
        ),
        review=sum(item.work_class is WorkClass.REVIEW for item in decisions),
        excluded=sum(item.work_class is WorkClass.EXCLUDED for item in decisions),
        malformed=sum(
            item.structural_reason is EligibilityReason.MALFORMED for item in decisions
        ),
    )


def snapshot_json(cards_json: str) -> str:
    """Validate a JSON Card array and return one deterministic JSON snapshot."""
    payload = json.loads(cards_json)
    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array of folded cards")
    cards = [Card.model_validate(item) for item in payload]
    return classify_structural_cards(cards).model_dump_json()
