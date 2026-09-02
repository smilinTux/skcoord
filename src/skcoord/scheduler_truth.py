"""SchedulerTruthV1: Canonical scheduler truth and exclusive eligibility reasons.

This module provides the versioned SKCoord scheduler-truth contract for structural
facts and runtime composition. It preserves historical events and legacy reads,
emits one exclusive primary reason plus diagnostic facets and aggregate invariants.

SKCoord owns shared contracts and structural facts. SKCapstone owns live worker,
backoff, host-routing, ITIL, and capacity facts.

The contract separates:
- Lifecycle state (open, claimed, complete, void, archived)
- Terminal disposition (done, blocked)
- Outcome (PASS, PASS_FOR_REVIEW, BLOCKED)
- Blocker (dependency, human, capability, card)
- Human decision (approval, override, escalation)
- Structural leaf (task, review, repair, escalation)
- Scheduler readiness (ready pool membership)
- Primary reason (exclusive eligibility/ineligibility)
- Diagnostic facets (auxiliary context)

Legacy label and verdict aliases remain readable. New writes use canonical vocabulary.
Historical events are never rewritten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# Canonical Vocabularies
# ============================================================================


class LifecycleState(str, Enum):
    """Card lifecycle states (SKCoord structural fact)."""

    OPEN = "open"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    VOID = "void"
    ARCHIVED = "archived"


class TerminalDisposition(str, Enum):
    """Terminal dispositions (derived from lifecycle and outcome)."""

    DONE = "done"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class VerdictOutcome(str, Enum):
    """Canonical verdict outcomes (evidence link values)."""

    PASS = "PASS"
    PASS_FOR_REVIEW = "PASS_FOR_REVIEW"
    BLOCKED = "BLOCKED"


class BlockerType(str, Enum):
    """Types of blockers preventing execution."""

    DEPENDENCY = "dependency"
    HUMAN = "human"
    CAPABILITY = "capability"
    CARD = "card"


class StructuralLeaf(str, Enum):
    """Structural card types that terminate a dependency chain."""

    TASK = "task"
    REVIEW = "review"
    REPAIR = "repair"
    ESCALATION = "escalation"


class PrimaryReason(str, Enum):
    """Exclusive primary reasons for scheduler eligibility/ineligibility.

    Every evaluated card has exactly one primary reason. The population equals
    ready cards plus all primary-reason counts.

    READY_POOL_REASONS: Cards that can be claimed by workers.
    INELIGIBLE_REASONS: Cards that cannot be claimed by workers.
    """

    # Ready pool reasons
    READY_NO_DEPENDENCIES = "ready_no_dependencies"
    READY_DEPENDENCIES_COMPLETE = "ready_dependencies_complete"
    READY_HUMAN_APPROVED = "ready_human_approved"

    # Ineligible reasons
    BLOCKED_DEPENDENCY_INCOMPLETE = "blocked_dependency_incomplete"
    BLOCKED_HUMAN_DECISION_PENDING = "blocked_human_decision_pending"
    BLOCKED_HUMAN_DECISION_DENIED = "blocked_human_decision_denied"
    BLOCKED_CAPABILITY_INSUFFICIENT = "blocked_capability_insufficient"
    BLOCKED_CARD_UNSATISFIABLE = "blocked_card_unsatisfiable"
    BLOCKED_TERMINAL_COMPLETE = "blocked_terminal_complete"
    BLOCKED_TERMINAL_VOID = "blocked_terminal_void"
    BLOCKED_TERMINAL_ARCHIVED = "blocked_terminal_archived"
    BLOCKED_CLAIMED_BY_OTHER = "blocked_claimed_by_other"
    BLOCKED_LAUNCH_BACKOFF = "blocked_launch_backoff"
    BLOCKED_LIFECYCLE_EXCLUDED = "blocked_lifecycle_excluded"


class DiagnosticFacet(str, Enum):
    """Auxiliary diagnostic context for primary reasons.

    Facets provide additional context without being exclusive. A card may have
    zero or more facets in addition to its single primary reason.
    """

    # Dependency facets
    HAS_DEPENDENCIES = "has_dependencies"
    DEPENDENCY_CYCLE_DETECTED = "dependency_cycle_detected"
    STALE_EXECUTION_BLOCKED = "stale_execution_blocked"

    # Human decision facets
    HUMAN_GATE = "human_gate"
    HUMAN_OVERRIDE = "human_override"
    ESCALATION_REQUIRED = "escalation_required"

    # Execution facets
    CLAIMED = "claimed"
    OWNER_UNKNOWN = "owner_unknown"
    LAUNCH_FAILED = "launch_failed"
    LAUNCH_TIMEOUT = "launch_timeout"

    # Quality facets
    VERDICT_PASS = "verdict_pass"
    VERDICT_PASS_FOR_REVIEW = "verdict_pass_for_review"
    VERDICT_BLOCKED = "verdict_blocked"
    INDEPENDENT_REVIEW_COMPLETE = "independent_review_complete"

    # Historical facets
    LEGACY_ONLY_STATE = "legacy_only_state"
    LEGACY_VERDICT_ALIAS = "legacy_verdict_alias"


# ============================================================================
# Legacy Verdict Alias Mapping
# ============================================================================


_LEGACY_VERDICT_PATTERNS = [
    # PASS_FOR_REVIEW must come before PASS to avoid "pass" matching "pass for review"
    (VerdictOutcome.PASS_FOR_REVIEW, [
        r"pass for review",
        r"pass review",
        r"needs review",
        r"review required",
        r"ready for review",
    ]),
    # PASS aliases
    (VerdictOutcome.PASS, [
        r"pass",
        r"approved",
        r"accepted",
        r"success",
        r"complete",
        r"done",
        r"landed",
        r"merged",
    ]),
    # BLOCKED aliases
    (VerdictOutcome.BLOCKED, [
        r"blocked",
        r"block",
        r"blocked on",
        r"depends on",
        r"waiting for",
        r"awaiting",
        r"blocked by",
    ]),
]


def _normalize_verdict_text(text: str) -> VerdictOutcome | None:
    """Normalize legacy verdict text to canonical outcome.

    Recognizes common legacy spellings and aliases. Returns None for text
    that does not match any known verdict pattern.
    """
    if not text:
        return None

    normalized = text.strip().lower()
    # Normalize underscores and dashes to spaces for comparison
    normalized = re.sub(r'[_-]', ' ', normalized)
    
    # Check patterns for legacy aliases (order matters: PASS_FOR_REVIEW before PASS)
    for outcome, patterns in _LEGACY_VERDICT_PATTERNS:
        for pattern in patterns:
            # Also normalize pattern to use spaces
            pattern_norm = re.sub(r'[_-]', ' ', pattern)
            if re.search(pattern_norm, normalized):
                return outcome
    return None


# ============================================================================
# Scheduler Truth V1 Model
# ============================================================================


@dataclass(frozen=True)
class HumanDecision:
    """Human decision recorded on a card."""

    decision: str
    decision_ref: str | None = None
    decision_sha256: str | None = None
    card_revision: int | None = None
    recorded_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "decision_ref": self.decision_ref,
            "decision_sha256": self.decision_sha256,
            "card_revision": self.card_revision,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class Blocker:
    """A blocker preventing card execution."""

    blocker_type: BlockerType
    referent: str
    description: str | None = None
    recorded_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocker_type": self.blocker_type.value,
            "referent": self.referent,
            "description": self.description,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class Verdict:
    """Canonical verdict with evidence reference."""

    outcome: VerdictOutcome
    evidence_sha256: str | None = None
    evidence_path: str | None = None
    recorded_at: str | None = None
    legacy_alias: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "evidence_sha256": self.evidence_sha256,
            "evidence_path": self.evidence_path,
            "recorded_at": self.recorded_at,
            "legacy_alias": self.legacy_alias,
        }


@dataclass(frozen=True)
class SchedulerTruthV1:
    """Versioned scheduler truth for one card.

    This is the canonical SKCoord contract for structural facts. SKCapstone
    composes this with live worker, backoff, host-routing, ITIL, and capacity
    facts to produce the complete scheduler decision.

    Invariant: Exactly one primary_reason is set, and it is mutually exclusive
    with all other PrimaryReason values. The population of cards equals the
    count of cards with primary_reason in READY_POOL_REASONS plus the sum of
    cards with each non-ready primary reason.
    """

    # Structural facts (SKCoord)
    card_id: str
    lifecycle: LifecycleState
    terminal_disposition: TerminalDisposition | None = None
    structural_leaf: StructuralLeaf = StructuralLeaf.TASK
    dependencies: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    # Outcome and verdict (evidence links)
    verdict: Verdict | None = None

    # Blockers
    blocker: Blocker | None = None

    # Human decision
    human_decision: HumanDecision | None = None

    # Scheduler facts (SKCoord + SKCapstone composition)
    scheduler_ready: bool = False
    primary_reason: PrimaryReason | None = None
    diagnostic_facets: tuple[DiagnosticFacet, ...] = ()

    # Runtime composition (SKCapstone)
    claim_owner: str | None = None
    claim_revision: int | None = None
    claim_timestamp: str | None = None
    launch_count: int = 0
    launch_backoff_until: str | None = None

    # Metadata
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    card_revision: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-serializable dict."""
        return {
            "schema": "skcoord.scheduler-truth/v1",
            "card_id": self.card_id,
            "lifecycle": self.lifecycle.value,
            "terminal_disposition": self.terminal_disposition.value if self.terminal_disposition else None,
            "structural_leaf": self.structural_leaf.value,
            "dependencies": list(self.dependencies),
            "labels": list(self.labels),
            "verdict": self.verdict.as_dict() if self.verdict else None,
            "blocker": self.blocker.as_dict() if self.blocker else None,
            "human_decision": self.human_decision.as_dict() if self.human_decision else None,
            "scheduler_ready": self.scheduler_ready,
            "primary_reason": self.primary_reason.value if self.primary_reason else None,
            "diagnostic_facets": [f.value for f in self.diagnostic_facets],
            "claim_owner": self.claim_owner,
            "claim_revision": self.claim_revision,
            "claim_timestamp": self.claim_timestamp,
            "launch_count": self.launch_count,
            "launch_backoff_until": self.launch_backoff_until,
            "evaluated_at": self.evaluated_at,
            "card_revision": self.card_revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerTruthV1:
        """Deserialize from dict."""
        verdict_data = data.get("verdict")
        verdict = None
        if verdict_data:
            verdict = Verdict(
                outcome=VerdictOutcome(verdict_data["outcome"]),
                evidence_sha256=verdict_data.get("evidence_sha256"),
                evidence_path=verdict_data.get("evidence_path"),
                recorded_at=verdict_data.get("recorded_at"),
                legacy_alias=verdict_data.get("legacy_alias"),
            )

        blocker_data = data.get("blocker")
        blocker = None
        if blocker_data:
            blocker = Blocker(
                blocker_type=BlockerType(blocker_data["blocker_type"]),
                referent=blocker_data["referent"],
                description=blocker_data.get("description"),
                recorded_at=blocker_data.get("recorded_at"),
            )

        human_decision_data = data.get("human_decision")
        human_decision = None
        if human_decision_data:
            human_decision = HumanDecision(
                decision=human_decision_data["decision"],
                decision_ref=human_decision_data.get("decision_ref"),
                decision_sha256=human_decision_data.get("decision_sha256"),
                card_revision=human_decision_data.get("card_revision"),
                recorded_at=human_decision_data.get("recorded_at"),
            )

        return cls(
            card_id=data["card_id"],
            lifecycle=LifecycleState(data["lifecycle"]),
            terminal_disposition=TerminalDisposition(data["terminal_disposition"]) if data.get("terminal_disposition") else None,
            structural_leaf=StructuralLeaf(data.get("structural_leaf", "task")),
            dependencies=tuple(data.get("dependencies", [])),
            labels=tuple(data.get("labels", [])),
            verdict=verdict,
            blocker=blocker,
            human_decision=human_decision,
            scheduler_ready=data.get("scheduler_ready", False),
            primary_reason=PrimaryReason(data["primary_reason"]) if data.get("primary_reason") else None,
            diagnostic_facets=tuple(DiagnosticFacet(f) for f in data.get("diagnostic_facets", [])),
            claim_owner=data.get("claim_owner"),
            claim_revision=data.get("claim_revision"),
            claim_timestamp=data.get("claim_timestamp"),
            launch_count=data.get("launch_count", 0),
            launch_backoff_until=data.get("launch_backoff_until"),
            evaluated_at=data.get("evaluated_at"),
            card_revision=data.get("card_revision"),
        )

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, json_str: str) -> SchedulerTruthV1:
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# Scheduler Truth Evaluator
# ============================================================================


@dataclass
class SchedulerTruthEvaluator:
    """Evaluates scheduler truth from card state and runtime facts."""

    home: Path

    def evaluate(
        self,
        card_id: str,
        lifecycle: LifecycleState,
        dependencies: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        claim_owner: str | None = None,
        claim_revision: int | None = None,
        claim_timestamp: str | None = None,
        verdict_text: str | None = None,
        verdict_evidence_sha256: str | None = None,
        human_decision: HumanDecision | None = None,
        launch_count: int = 0,
        launch_backoff_until: str | None = None,
        complete_dependencies: set[str] | None = None,
    ) -> SchedulerTruthV1:
        """Evaluate scheduler truth for a card.

        Args:
            card_id: Card identifier
            lifecycle: Current lifecycle state
            dependencies: Card dependency IDs
            labels: Card labels
            claim_owner: Current claim owner if any
            claim_revision: Current claim revision if any
            claim_timestamp: Current claim timestamp if any
            verdict_text: Verdict text (legacy or canonical)
            verdict_evidence_sha256: SHA256 of verdict evidence artifact
            human_decision: Recorded human decision if any
            launch_count: Number of times this card has been launched
            launch_backoff_until: ISO timestamp when backoff expires
            complete_dependencies: Set of dependency card IDs that are complete

        Returns:
            SchedulerTruthV1 with exclusive primary reason and diagnostic facets
        """
        complete_dependencies = complete_dependencies or set()

        # Determine structural leaf type
        structural_leaf = self._infer_structural_leaf(card_id, labels, lifecycle)

        # Determine terminal disposition
        terminal_disposition = self._determine_terminal_disposition(lifecycle, verdict_text)

        # Normalize verdict
        verdict = self._normalize_verdict(verdict_text, verdict_evidence_sha256)

        # Determine blocker
        blocker = self._determine_blocker(verdict, human_decision)

        # Determine primary reason and scheduler readiness
        primary_reason, diagnostic_facets = self._determine_primary_reason(
            lifecycle=lifecycle,
            dependencies=dependencies,
            complete_dependencies=complete_dependencies,
            labels=labels,
            claim_owner=claim_owner,
            verdict=verdict,
            human_decision=human_decision,
            launch_count=launch_count,
            launch_backoff_until=launch_backoff_until,
            structural_leaf=structural_leaf,
        )

        # Scheduler readiness is true only for ready pool reasons
        scheduler_ready = primary_reason in {
            PrimaryReason.READY_NO_DEPENDENCIES,
            PrimaryReason.READY_DEPENDENCIES_COMPLETE,
            PrimaryReason.READY_HUMAN_APPROVED,
        }

        return SchedulerTruthV1(
            card_id=card_id,
            lifecycle=lifecycle,
            terminal_disposition=terminal_disposition,
            structural_leaf=structural_leaf,
            dependencies=dependencies,
            labels=labels,
            verdict=verdict,
            blocker=blocker,
            human_decision=human_decision,
            scheduler_ready=scheduler_ready,
            primary_reason=primary_reason,
            diagnostic_facets=diagnostic_facets,
            claim_owner=claim_owner,
            claim_revision=claim_revision,
            claim_timestamp=claim_timestamp,
            launch_count=launch_count,
            launch_backoff_until=launch_backoff_until,
        )

    def _infer_structural_leaf(
        self, card_id: str, labels: tuple[str, ...], lifecycle: LifecycleState
    ) -> StructuralLeaf:
        """Infer structural leaf type from card ID and labels."""
        card_lower = card_id.lower()

        if "[review]" in card_lower or "[rereview]" in card_lower or "review" in labels:
            return StructuralLeaf.REVIEW
        if "[repair]" in card_lower or "repair" in labels:
            return StructuralLeaf.REPAIR
        if "[escalation]" in card_lower or "escalation" in labels:
            return StructuralLeaf.ESCALATION

        return StructuralLeaf.TASK

    def _determine_terminal_disposition(
        self, lifecycle: LifecycleState, verdict_text: str | None
    ) -> TerminalDisposition | None:
        """Determine terminal disposition from lifecycle and verdict."""
        if lifecycle == LifecycleState.COMPLETE:
            if verdict_text and "blocked" in verdict_text.lower():
                return TerminalDisposition.BLOCKED
            return TerminalDisposition.DONE
        if lifecycle == LifecycleState.VOID:
            return TerminalDisposition.BLOCKED
        if lifecycle == LifecycleState.ARCHIVED:
            # Archived doesn't have a terminal disposition in V1
            return None

        return None

    def _normalize_verdict(
        self, verdict_text: str | None, evidence_sha256: str | None
    ) -> Verdict | None:
        """Normalize verdict text to canonical outcome."""
        if not verdict_text:
            return None

        outcome = _normalize_verdict_text(verdict_text)
        if not outcome:
            return None

        legacy_alias = verdict_text if outcome != VerdictOutcome.BLOCKED else None

        return Verdict(
            outcome=outcome,
            evidence_sha256=evidence_sha256,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            legacy_alias=legacy_alias,
        )

    def _determine_blocker(
        self, verdict: Verdict | None, human_decision: HumanDecision | None
    ) -> Blocker | None:
        """Determine blocker from verdict and human decision."""
        if verdict and verdict.outcome == VerdictOutcome.BLOCKED:
            # Parse BLOCKED verdict contract format
            if verdict.legacy_alias:
                # Look for blocked_on pattern
                match = re.search(
                    r"blocked[_-]?on\s*=\s*(\w+)",
                    verdict.legacy_alias,
                    re.IGNORECASE,
                )
                if match:
                    blocker_type_str = match.group(1).lower()
                    referent_match = re.search(
                        r"referent\s*=\s*(\S+)",
                        verdict.legacy_alias,
                        re.IGNORECASE,
                    )
                    referent = referent_match.group(1) if referent_match else "unknown"

                    try:
                        blocker_type = BlockerType(blocker_type_str)
                        return Blocker(
                            blocker_type=blocker_type,
                            referent=referent,
                            description=verdict.legacy_alias,
                            recorded_at=verdict.recorded_at,
                        )
                    except ValueError:
                        pass

        # Check for human decision blocker
        if human_decision and human_decision.decision.lower() in ("denied", "rejected"):
            return Blocker(
                blocker_type=BlockerType.HUMAN,
                referent=human_decision.decision_ref or "human_decision",
                description=f"Human decision: {human_decision.decision}",
                recorded_at=human_decision.recorded_at,
            )

        return None

    def _determine_primary_reason(
        self,
        lifecycle: LifecycleState,
        dependencies: tuple[str, ...],
        complete_dependencies: set[str],
        labels: tuple[str, ...],
        claim_owner: str | None,
        verdict: Verdict | None,
        human_decision: HumanDecision | None,
        launch_count: int,
        launch_backoff_until: str | None,
        structural_leaf: StructuralLeaf,
    ) -> tuple[PrimaryReason | None, tuple[DiagnosticFacet, ...]]:
        """Determine exclusive primary reason and diagnostic facets.

        This is the core scheduler logic. Exactly one primary reason is returned,
        which determines scheduler readiness. Diagnostic facets provide additional
        context without affecting the primary decision.
        """
        facets: list[DiagnosticFacet] = []

        # Terminal states take precedence
        if lifecycle == LifecycleState.COMPLETE:
            facets.append(DiagnosticFacet.VERDICT_PASS)
            return PrimaryReason.BLOCKED_TERMINAL_COMPLETE, tuple(facets)

        if lifecycle == LifecycleState.VOID:
            return PrimaryReason.BLOCKED_TERMINAL_VOID, tuple(facets)

        if lifecycle == LifecycleState.ARCHIVED:
            return PrimaryReason.BLOCKED_TERMINAL_ARCHIVED, tuple(facets)

        # Check for human decision blockers
        if human_decision:
            decision_lower = human_decision.decision.lower()
            if decision_lower in ("denied", "rejected"):
                facets.append(DiagnosticFacet.HUMAN_GATE)
                return PrimaryReason.BLOCKED_HUMAN_DECISION_DENIED, tuple(facets)
            elif decision_lower in ("pending", "awaiting"):
                facets.append(DiagnosticFacet.HUMAN_GATE)
                return PrimaryReason.BLOCKED_HUMAN_DECISION_PENDING, tuple(facets)
            elif decision_lower in ("approved", "granted"):
                facets.append(DiagnosticFacet.HUMAN_GATE)
                # Fall through to check other eligibility criteria

        # Check for claim by other agent
        if claim_owner and claim_owner != "pi-glm-chiap02-1f706c4a":
            facets.append(DiagnosticFacet.CLAIMED)
            return PrimaryReason.BLOCKED_CLAIMED_BY_OTHER, tuple(facets)

        # Check for launch backoff
        if launch_backoff_until:
            try:
                backoff_time = datetime.fromisoformat(launch_backoff_until)
                if backoff_time > datetime.now(timezone.utc):
                    facets.append(DiagnosticFacet.LAUNCH_FAILED)
                    return PrimaryReason.BLOCKED_LAUNCH_BACKOFF, tuple(facets)
            except (ValueError, TypeError):
                # Invalid backoff timestamp, ignore
                pass

        # Check dependencies
        if dependencies:
            facets.append(DiagnosticFacet.HAS_DEPENDENCIES)
            incomplete = [d for d in dependencies if d not in complete_dependencies]
            if incomplete:
                return PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE, tuple(facets)
            else:
                # All dependencies complete
                facets.append(DiagnosticFacet.VERDICT_PASS)
                if human_decision and human_decision.decision.lower() in (
                    "approved",
                    "granted",
                ):
                    return PrimaryReason.READY_HUMAN_APPROVED, tuple(facets)
                return PrimaryReason.READY_DEPENDENCIES_COMPLETE, tuple(facets)

        # No dependencies
        if verdict and verdict.outcome == VerdictOutcome.BLOCKED:
            if verdict.legacy_alias and "capability" in verdict.legacy_alias.lower():
                return PrimaryReason.BLOCKED_CAPABILITY_INSUFFICIENT, tuple(facets)
            if verdict.legacy_alias and "card" in verdict.legacy_alias.lower():
                return PrimaryReason.BLOCKED_CARD_UNSATISFIABLE, tuple(facets)

        # Ready with no dependencies
        facets.append(DiagnosticFacet.VERDICT_PASS)
        return PrimaryReason.READY_NO_DEPENDENCIES, tuple(facets)


# ============================================================================
# Operator Reason Table
# ============================================================================

REASON_TABLE: dict[PrimaryReason, dict[str, str]] = {
    # Ready pool reasons
    PrimaryReason.READY_NO_DEPENDENCIES: {
        "description": "Card has no dependencies and can be claimed",
        "action": "Claim card for execution",
        "operator_action": "Monitor assignment or increase worker capacity",
    },
    PrimaryReason.READY_DEPENDENCIES_COMPLETE: {
        "description": "All dependencies are complete and card can be claimed",
        "action": "Claim card for execution",
        "operator_action": "Monitor assignment or increase worker capacity",
    },
    PrimaryReason.READY_HUMAN_APPROVED: {
        "description": "Human approval granted and dependencies complete",
        "action": "Claim card for execution",
        "operator_action": "Monitor assignment or increase worker capacity",
    },
    # Ineligible reasons
    PrimaryReason.BLOCKED_DEPENDENCY_INCOMPLETE: {
        "description": "One or more dependencies are not complete",
        "action": "Do not claim (dependency incomplete)",
        "operator_action": "Check dependency card status and expedite if needed",
    },
    PrimaryReason.BLOCKED_HUMAN_DECISION_PENDING: {
        "description": "Awaiting human approval or decision",
        "action": "Do not claim (awaiting human decision)",
        "operator_action": "Provide human decision or escalate",
    },
    PrimaryReason.BLOCKED_HUMAN_DECISION_DENIED: {
        "description": "Human decision denied card execution",
        "action": "Do not claim (human decision denied)",
        "operator_action": "Review denial and provide override if appropriate",
    },
    PrimaryReason.BLOCKED_CAPABILITY_INSUFFICIENT: {
        "description": "Card requires capability not available to current agent",
        "action": "Do not claim (assign to stronger agent)",
        "operator_action": "Assign to agent with required capability",
    },
    PrimaryReason.BLOCKED_CARD_UNSATISFIABLE: {
        "description": "Card criteria cannot be satisfied as written",
        "action": "Do not claim (card must be revised)",
        "operator_action": "Revise card criteria or void the card",
    },
    PrimaryReason.BLOCKED_TERMINAL_COMPLETE: {
        "description": "Card is complete and done",
        "action": "Do not claim (terminal)",
        "operator_action": "Archive if appropriate",
    },
    PrimaryReason.BLOCKED_TERMINAL_VOID: {
        "description": "Card is voided and cannot be executed",
        "action": "Do not claim (terminal)",
        "operator_action": "None (void is terminal)",
    },
    PrimaryReason.BLOCKED_TERMINAL_ARCHIVED: {
        "description": "Card is archived and no longer active",
        "action": "Do not claim (terminal)",
        "operator_action": "None (archive is terminal)",
    },
    PrimaryReason.BLOCKED_CLAIMED_BY_OTHER: {
        "description": "Card is claimed by another agent",
        "action": "Do not claim (respect existing claim)",
        "operator_action": "Monitor claim for timeout or failure",
    },
    PrimaryReason.BLOCKED_LAUNCH_BACKOFF: {
        "description": "Card is in launch backoff after failed attempts",
        "action": "Do not claim (in backoff)",
        "operator_action": "Review failure logs and investigate root cause",
    },
    PrimaryReason.BLOCKED_LIFECYCLE_EXCLUDED: {
        "description": "Card excluded by lifecycle assessment",
        "action": "Do not claim (excluded by policy)",
        "operator_action": "Review lifecycle assessment and exclusion criteria",
    },
}


def get_reason_description(reason: PrimaryReason) -> str:
    """Get human-readable description for a primary reason."""
    return REASON_TABLE.get(reason, {}).get("description", "Unknown reason")


def get_reason_action(reason: PrimaryReason) -> str:
    """Get the action (what workers should do) for a primary reason."""
    return REASON_TABLE.get(reason, {}).get("action", "Unknown action")


def get_operator_action(reason: PrimaryReason) -> str:
    """Get the operator action (what humans should do) for a primary reason."""
    return REASON_TABLE.get(reason, {}).get("operator_action", "Unknown action")


# ============================================================================
# JSON CLI Helpers
# ============================================================================


def format_scheduler_truth_json(truth: SchedulerTruthV1, pretty: bool = True) -> str:
    """Format scheduler truth as JSON."""
    if pretty:
        return json.dumps(truth.as_dict(), indent=2, sort_keys=True)
    return truth.to_json()


def print_reason_table() -> None:
    """Print the operator reason table to stdout."""
    print("# Scheduler Truth V1 - Operator Reason Table\n")
    print("| Primary Reason | Description | Worker Action | Operator Action |")
    print("|---------------|-------------|---------------|-----------------|")
    for reason in PrimaryReason:
        table = REASON_TABLE.get(reason, {})
        print(
            f"| {reason.value:45} | {table.get('description', 'N/A'):60} | "
            f"{table.get('action', 'N/A'):40} | {table.get('operator_action', 'N/A'):45} |"
        )
