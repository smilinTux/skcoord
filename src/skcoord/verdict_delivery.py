"""Transactional review verdict recording for CardStore.

This module provides the worker command that records the exact non-provisional
review verdict, evidence reference and hash, reviewer identity, and terminal
review state transactionally. SKMail is notification after durable board success,
never a verdict substitute.

The design addresses the review-loop gap demonstrated by card 0abdb524: the
reviewer completed a PASS and mailed it, but the board retained no verdict,
so the review and parent cannot close.

Key invariants:
1. One supported command validates all inputs before mutation
2. A single CAS-governed operation records verdict evidence and terminal state
3. Notification runs only after durable CardStore readback
4. Mail text alone is never folded as approval or verdict authority
5. All validation happens before any write, ensuring fail-loudly semantics
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .card_store import CardStore

logger = logging.getLogger(__name__)

# Valid terminal verdict values
_VALID_VERDICTS = {"PASS", "PASS_FOR_REVIEW", "BLOCKED"}

# SHA256 regex - exactly 64 hex characters
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Blocked verdict categories
_BLOCKED_CATEGORIES = {"dependency", "human", "capability", "card"}


class VerdictRecord(BaseModel):
    """A transactional verdict record with all required validation.

    This model represents the complete verdict evidence that must be recorded
    atomically. Validation happens at construction time, so if a VerdictRecord
    exists, all preconditions are satisfied.
    """

    review_card_id: str = Field(..., description="The review card being recorded")
    parent_card_id: str = Field(..., description="The parent card under review")
    verdict: str = Field(..., description="Terminal verdict: PASS, PASS_FOR_REVIEW, or BLOCKED")
    evidence_uri: str = Field(..., description="URI to the evidence file (file:// or https://)")
    evidence_sha256: str = Field(..., description="SHA256 hash of the evidence content")
    reviewer_identity: str = Field(..., description="Full agent identity (e.g., pi-glm-chiap02-b8d4e2a1)")
    claim_revision: str = Field(..., description="The claim_revision from the current claim")
    blocked_on: str | None = Field(default=None, description="For BLOCKED: dependency, human, capability, or card")
    blocked_referent: str | None = Field(default=None, description="For BLOCKED: exact referent (card:xxx, approval:xxx, etc.)")
    attempted: list[str] = Field(default_factory=list, description="What the reviewer attempted (for BLOCKED on capability)")
    pr_url: str | None = Field(default=None, description="Pull request URL if applicable")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("review_card_id", "parent_card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Card IDs must be non-empty and not path traversal."""
        if not v or not isinstance(v, str):
            raise ValueError("card_id must be a non-empty string")
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError(f"card_id {v!r} contains path traversal")
        if len(v) > 128:
            raise ValueError(f"card_id {v!r} exceeds maximum length 128")
        return v

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, v: str) -> str:
        """Verdict must be one of the supported terminal values."""
        v_upper = str(v).upper().strip()
        if v_upper not in _VALID_VERDICTS:
            raise ValueError(
                f"verdict must be one of {_VALID_VERDICTS}, got {v!r}"
            )
        return v_upper

    @field_validator("evidence_sha256")
    @classmethod
    def validate_sha256(cls, v: str) -> str:
        """Evidence SHA256 must be exactly 64 hex characters."""
        v_clean = str(v).strip().lower()
        if not _SHA256_RE.match(v_clean):
            raise ValueError(
                f"evidence_sha256 must be exactly 64 hex characters, got {v!r}"
            )
        return v_clean

    @field_validator("evidence_uri")
    @classmethod
    def validate_evidence_uri(cls, v: str) -> str:
        """Evidence URI must be file:// or https://."""
        v_str = str(v).strip()
        if not v_str:
            raise ValueError("evidence_uri cannot be empty")
        if not (v_str.startswith("file://") or v_str.startswith("https://")):
            raise ValueError(
                f"evidence_uri must start with file:// or https://, got {v!r}"
            )
        return v_str

    @field_validator("blocked_on")
    @classmethod
    def validate_blocked_on(cls, v: str | None, info) -> str | None:
        """For BLOCKED verdicts, blocked_on must be a valid category."""
        verdict = info.data.get("verdict", "")
        if v is None:
            # If verdict is BLOCKED, blocked_on is required
            if verdict == "BLOCKED":
                raise ValueError(
                    "BLOCKED verdict requires blocked_on "
                    f"(one of: {', '.join(sorted(_BLOCKED_CATEGORIES))})"
                )
            return None
        v_lower = str(v).lower().strip()
        if v_lower not in _BLOCKED_CATEGORIES:
            raise ValueError(
                f"blocked_on must be one of {_BLOCKED_CATEGORIES}, got {v!r}"
            )
        return v_lower

    @field_validator("blocked_referent")
    @classmethod
    def validate_blocked_referent(cls, v: str | None, info) -> str | None:
        """For BLOCKED verdicts, blocked_referent must identify something real."""
        if v is None:
            blocked_on = info.data.get("blocked_on")
            if blocked_on in _BLOCKED_CATEGORIES:
                raise ValueError(
                    f"blocked_on={blocked_on} requires a blocked_referent "
                    "(e.g., card:xxx, approval:xxx, ac:N, or free-form description)"
                )
            return None
        v_str = str(v).strip()
        if not v_str:
            raise ValueError("blocked_referent cannot be empty when blocked_on is set")
        # For dependency and card, referent must be a card id
        blocked_on = info.data.get("blocked_on")
        if blocked_on in {"dependency", "card"}:
            # Accept both plain card IDs and card:<id> format
            # Card ID pattern: 8+ hex chars, optional kind prefix, optional card: prefix
            # First, strip the card: prefix if present for validation
            test_str = v_str
            if test_str.lower().startswith("card:"):
                test_str = test_str[5:].lstrip()
            if test_str.lower().startswith("card "):
                test_str = test_str[5:].lstrip()
            
            card_id_re = re.compile(r"^(?:[a-z]{3}-)?[0-9a-f]{8,}$", re.IGNORECASE)
            if not card_id_re.match(test_str):
                raise ValueError(
                    f"blocked_on={blocked_on} requires a card id as referent, "
                    f"got {v!r}"
                )
            # Return the stripped version for storage
            return test_str
        return v_str

    @field_validator("attempted")
    @classmethod
    def validate_attempted(cls, v: list[str]) -> list[str]:
        """Attempted must be a list of non-empty strings."""
        if not isinstance(v, list):
            raise ValueError("attempted must be a list")
        return [str(item).strip() for item in v if str(item).strip()]

    @model_validator(mode="after")
    def validate_blocked_requirements(self) -> "VerdictRecord":
        """Cross-field validation for BLOCKED verdict requirements."""
        if self.verdict == "BLOCKED":
            if self.blocked_on is None:
                raise ValueError(
                    "BLOCKED verdict requires blocked_on "
                    f"(one of: {', '.join(sorted(_BLOCKED_CATEGORIES))})"
                )
            if self.blocked_referent is None:
                raise ValueError(
                    f"blocked_on={self.blocked_on} requires a blocked_referent "
                    "(e.g., card:xxx, approval:xxx, ac:N, or free-form description)"
                )
        return self


class VerdictDeliveryError(Exception):
    """Raised when verdict delivery fails after any write attempt.

    This exception indicates a partial failure state that must be surfaced
    to the caller so the verdict can be retried or manually reconciled.
    """

    def __init__(self, message: str, partial_event: dict | None = None):
        super().__init__(message)
        self.partial_event = partial_event


class VerdictDelivery:
    """Transactional verdict delivery to CardStore with notification.

    This class implements the single supported worker command for recording
    review verdicts. It validates all preconditions, writes a single atomic
    event to CardStore, reads back to confirm durability, then sends notifications.

    The design ensures:
    1. No partial writes - either the full verdict is recorded or nothing
    2. Notifications happen only after successful readback
    3. A notification failure cannot erase the board verdict
    """

    def __init__(self, home: Path):
        """Initialize verdict delivery with the coordination home.

        Args:
            home: Path to the coordination directory (e.g., ~/.skcapstone)
        """
        self.home = Path(home).expanduser()
        self.store = CardStore(self.home)

    def _validate_review_card(
        self, review_card_id: str, parent_card_id: str, reviewer_identity: str
    ) -> tuple[dict, str]:
        """Validate that the review card exists, has the right parent, and is claimed.

        Args:
            review_card_id: The review card ID
            parent_card_id: The parent card ID
            reviewer_identity: The reviewer's full agent identity

        Returns:
            tuple: (review_card_dict, current_claim_revision)

        Raises:
            ValueError: If any validation fails
        """
        # Load the review card
        review_card = self.store.fold(review_card_id)
        if review_card is None:
            raise ValueError(f"review card {review_card_id} does not exist in CardStore")

        # Verify it's actually a review card (has parent tag or title marker)
        parent_tags = [t for t in review_card.labels if t.startswith("parent-")]
        if not parent_tags:
            # Check title for [REVIEW] marker
            if "[REVIEW]" not in review_card.title.upper():
                raise ValueError(
                    f"card {review_card_id} is not marked as a review: "
                    "no parent tag and no [REVIEW] in title"
                )
            # Extract parent from description if possible
            if parent_card_id not in review_card.description:
                raise ValueError(
                    f"review card {review_card_id} does not mention parent {parent_card_id}"
                )
        else:
            # Verify parent tag matches
            expected_tag = f"parent-{parent_card_id}"
            if expected_tag not in parent_tags:
                actual_parents = ", ".join(parent_tags)
                raise ValueError(
                    f"review card {review_card_id} has parent tags [{actual_parents}] "
                    f"but expected parent-{parent_card_id}"
                )

        # Verify the reviewer is the current claimant
        if review_card.owner != reviewer_identity:
            raise ValueError(
                f"review card {review_card_id} is claimed by {review_card.owner}, "
                f"not by {reviewer_identity}"
            )

        # Get the current claim_revision
        claim_revision = review_card.meta.get("_claim_revision")
        if not claim_revision:
            raise ValueError(
                f"review card {review_card_id} has no claim_revision in metadata"
            )

        return {
            "id": review_card.id,
            "title": review_card.title,
            "owner": review_card.owner,
            "status": review_card.status.value,
            "claim_revision": claim_revision,
        }, claim_revision

    def _validate_evidence(self, evidence_uri: str, evidence_sha256: str) -> None:
        """Validate that the evidence file exists and has the claimed hash.

        Args:
            evidence_uri: URI to the evidence file
            evidence_sha256: Expected SHA256 hash

        Raises:
            ValueError: If evidence file doesn't exist or hash doesn't match
        """
        # Only validate file:// URIs (https:// is assumed externally verified)
        if not evidence_uri.startswith("file://"):
            logger.info(f"Skipping hash validation for non-file URI: {evidence_uri}")
            return

        # Extract file path from file:// URI
        file_path = evidence_uri[7:]  # Remove "file://" prefix
        evidence_path = Path(file_path).expanduser()

        if not evidence_path.exists():
            raise ValueError(
                f"evidence file does not exist: {evidence_path}"
            )

        # Compute SHA256 of the evidence file
        sha256_hash = hashlib.sha256()
        with open(evidence_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        actual_hash = sha256_hash.hexdigest()

        if actual_hash.lower() != evidence_sha256.lower():
            raise ValueError(
                f"evidence hash mismatch: expected {evidence_sha256}, "
                f"got {actual_hash} for {evidence_path}"
            )

    def _send_skmail_notification(
        self, verdict_record: VerdictRecord, event_id: str
    ) -> None:
        """Send SKMail notifications after successful verdict recording.

        Notifications are sent to jarvis and lumina. A notification failure
        does NOT invalidate the verdict - the board record is authoritative.

        Args:
            verdict_record: The validated verdict record
            event_id: The CardStore event ID that recorded the verdict
        """
        try:
            skmail_dir = self.home / "coordination" / "skmail.d"
            skmail_dir.mkdir(parents=True, exist_ok=True)

            # Build notification message
            subject = f"[{verdict_record.verdict}] Review verdict for {verdict_record.parent_card_id}"

            body_parts = [
                f"Card: {verdict_record.review_card_id}",
                f"Verdict: {verdict_record.verdict}",
                f"Reviewer: {verdict_record.reviewer_identity}",
                f"Timestamp: {verdict_record.timestamp}",
                "",
                "=== EVIDENCE ===",
                f"URI: {verdict_record.evidence_uri}",
                f"SHA256: {verdict_record.evidence_sha256}",
            ]

            if verdict_record.verdict == "BLOCKED":
                body_parts.extend([
                    "",
                    "=== BLOCKED DETAILS ===",
                    f"Blocked on: {verdict_record.blocked_on}",
                    f"Referent: {verdict_record.blocked_referent}",
                ])
                if verdict_record.attempted:
                    body_parts.append("")
                    body_parts.append("=== ATTEMPTED ===")
                    body_parts.extend(f"- {item}" for item in verdict_record.attempted)

            if verdict_record.pr_url:
                body_parts.extend([
                    "",
                    "=== PULL REQUEST ===",
                    verdict_record.pr_url,
                ])

            body_parts.append("")
            body_parts.append("=== CARD STORE EVENT ===")
            body_parts.append(f"Event ID: {event_id}")
            body_parts.append("")
            body_parts.append("Note: This notification is informational only. "
                            "The authoritative verdict is recorded in CardStore.")

            body = "\n".join(body_parts)

            # Write notification to skmail.d
            # Use a deterministic filename for de-duplication
            safe_card_id = verdict_record.review_card_id.replace("/", "-")
            notification_filename = (
                f"{verdict_record.reviewer_identity}-verdict-{safe_card_id}-"
                f"{verdict_record.timestamp.replace(':', '-').replace('.', '-')}.jsonl"
            )
            notification_path = skmail_dir / notification_filename

            notification = {
                "event_id": event_id,
                "ts": verdict_record.timestamp,
                "writer": verdict_record.reviewer_identity,
                "node": verdict_record.reviewer_identity.split("@")[-1] if "@" in verdict_record.reviewer_identity else "unknown",
                "seq": 0,
                "action": "skmail",
                "to": ["jarvis", "lumina"],
                "subject": subject,
                "message": body,
                "card_id": verdict_record.review_card_id,
                "parent_id": verdict_record.parent_card_id,
                "verdict": verdict_record.verdict,
                "evidence_uri": verdict_record.evidence_uri,
                "evidence_sha256": verdict_record.evidence_sha256,
                "transition_id": f"verdict-notification-{event_id}",
            }

            # Atomic write using the same pattern as CardStore
            temp_path = notification_path.with_suffix(".tmp")
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(notification, default=str) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                # Atomic rename
                temp_path.replace(notification_path)
                logger.info(f"Verdict notification written to {notification_path}")
            except OSError as e:
                # Log but don't fail - the verdict is already recorded
                logger.error(f"Failed to write verdict notification: {e}")

        except Exception as e:
            # Log but don't fail - the verdict is already recorded in CardStore
            logger.error(f"Failed to send verdict notification (non-fatal): {e}")

    def record_verdict(
        self,
        review_card_id: str,
        parent_card_id: str,
        verdict: str,
        evidence_uri: str,
        evidence_sha256: str,
        reviewer_identity: str,
        claim_revision: str,
        blocked_on: str | None = None,
        blocked_referent: str | None = None,
        attempted: list[str] | None = None,
        pr_url: str | None = None,
    ) -> dict:
        """Record a review verdict transactionally to CardStore.

        This is the single supported worker command for recording review verdicts.
        It validates all inputs, writes a single atomic event to CardStore, reads
        back to confirm durability, then sends notifications.

        Args:
            review_card_id: The review card ID
            parent_card_id: The parent card under review
            verdict: Terminal verdict (PASS, PASS_FOR_REVIEW, or BLOCKED)
            evidence_uri: URI to the evidence file
            evidence_sha256: SHA256 hash of the evidence content
            reviewer_identity: Full agent identity (e.g., pi-glm-chiap02-b8d4e2a1)
            claim_revision: The claim_revision from the current claim
            blocked_on: For BLOCKED: dependency, human, capability, or card
            blocked_referent: For BLOCKED: exact referent
            attempted: For BLOCKED: what the reviewer attempted
            pr_url: Pull request URL if applicable

        Returns:
            dict: The CardStore event that was recorded

        Raises:
            ValueError: If any validation fails (before any write)
            VerdictDeliveryError: If the write or readback fails
        """
        import os

        # Step 1: Build and validate the verdict record (fail-fast, no writes)
        verdict_record = VerdictRecord(
            review_card_id=review_card_id,
            parent_card_id=parent_card_id,
            verdict=verdict,
            evidence_uri=evidence_uri,
            evidence_sha256=evidence_sha256,
            reviewer_identity=reviewer_identity,
            claim_revision=claim_revision,
            blocked_on=blocked_on,
            blocked_referent=blocked_referent,
            attempted=attempted or [],
            pr_url=pr_url,
        )

        # Step 2: Validate the review card state
        review_card_info, current_claim_revision = self._validate_review_card(
            review_card_id, parent_card_id, reviewer_identity
        )

        # Verify claim_revision matches
        if current_claim_revision != claim_revision:
            raise ValueError(
                f"claim_revision mismatch: card has {current_claim_revision}, "
                f"caller provided {claim_revision}"
            )

        # Step 3: Validate evidence file exists and has correct hash
        self._validate_evidence(evidence_uri, evidence_sha256)

        # Step 4: Build the CardStore event payload
        # We use a "link" action with "verdict" as the key, which the CardStore
        # fold already knows how to handle
        event_payload = {
            "transition_id": f"verdict-{review_card_id}-{claim_revision}",
            "review_card_id": review_card_id,
            "parent_card_id": parent_card_id,
            "verdict": verdict_record.verdict,
            "evidence_uri": verdict_record.evidence_uri,
            "evidence_sha256": verdict_record.evidence_sha256,
            "reviewer_identity": verdict_record.reviewer_identity,
        }

        # Add BLOCKED-specific fields
        if verdict_record.verdict == "BLOCKED":
            event_payload["blocked_on"] = verdict_record.blocked_on
            event_payload["blocked_referent"] = verdict_record.blocked_referent
            if verdict_record.attempted:
                event_payload["attempted"] = verdict_record.attempted

        if verdict_record.pr_url:
            event_payload["pr_url"] = verdict_record.pr_url

        # Step 5: Write to CardStore (single atomic operation)
        try:
            event = self.store.append_event(
                card_id=review_card_id,
                action="link",
                agent=reviewer_identity,
                link_key="verdict",
                link_value=json.dumps({
                    "verdict": verdict_record.verdict,
                    "evidence_uri": verdict_record.evidence_uri,
                    "evidence_sha256": verdict_record.evidence_sha256,
                    "reviewer": verdict_record.reviewer_identity,
                    "parent": parent_card_id,
                    "blocked_on": verdict_record.blocked_on,
                    "blocked_referent": verdict_record.blocked_referent,
                    "attempted": verdict_record.attempted,
                    "pr_url": verdict_record.pr_url,
                    "timestamp": verdict_record.timestamp,
                }),
                **event_payload,
            )
        except Exception as e:
            raise VerdictDeliveryError(
                f"Failed to write verdict event to CardStore: {e}",
                partial_event=None,
            ) from e

        event_id = event.get("event_id")
        if not event_id:
            raise VerdictDeliveryError(
                "CardStore returned event without event_id",
                partial_event=event,
            )

        # Step 6: Read back to confirm durability
        try:
            folded_card = self.store.fold(review_card_id)
            if folded_card is None:
                raise VerdictDeliveryError(
                    f"CardStore fold returned None for {review_card_id} after write",
                    partial_event=event,
                )

            # Verify the verdict link is present in the folded card
            verdict_value = folded_card.links.get("verdict")
            if not verdict_value:
                raise VerdictDeliveryError(
                    f"Verdict link not found in folded card {review_card_id} after write",
                    partial_event=event,
                )

            # Verify it's our verdict
            try:
                verdict_data = json.loads(verdict_value) if isinstance(verdict_value, str) else verdict_value
                if verdict_data.get("verdict") != verdict_record.verdict:
                    raise VerdictDeliveryError(
                        f"Verdict mismatch in folded card: expected {verdict_record.verdict}, "
                        f"got {verdict_data.get('verdict')}",
                        partial_event=event,
                    )
            except (json.JSONDecodeError, TypeError) as e:
                raise VerdictDeliveryError(
                    f"Failed to parse verdict from folded card: {e}",
                    partial_event=event,
                ) from e

            logger.info(
                f"Verdict {verdict_record.verdict} for {review_card_id} "
                f"confirmed durable in CardStore (event_id={event_id})"
            )

        except VerdictDeliveryError:
            raise
        except Exception as e:
            raise VerdictDeliveryError(
                f"Failed to read back verdict from CardStore: {e}",
                partial_event=event,
            ) from e

        # Step 7: Send notifications (failure does not invalidate the verdict)
        self._send_skmail_notification(verdict_record, event_id)

        logger.info(
            f"Verdict delivery complete: {verdict_record.verdict} for {review_card_id} "
            f"(parent={parent_card_id}, event_id={event_id})"
        )

        return event


def record_verdict_command(
    home: Path,
    review_card_id: str,
    parent_card_id: str,
    verdict: str,
    evidence_uri: str,
    evidence_sha256: str,
    reviewer_identity: str,
    claim_revision: str,
    blocked_on: str | None = None,
    blocked_referent: str | None = None,
    attempted: list[str] | None = None,
    pr_url: str | None = None,
) -> dict:
    """Convenience function for the worker command.

    This is the entry point for workers to record verdicts. All validation
    and transactional logic is handled by VerdictDelivery.

    Args:
        home: Path to the coordination directory
        review_card_id: The review card ID
        parent_card_id: The parent card under review
        verdict: Terminal verdict (PASS, PASS_FOR_REVIEW, or BLOCKED)
        evidence_uri: URI to the evidence file
        evidence_sha256: SHA256 hash of the evidence content
        reviewer_identity: Full agent identity
        claim_revision: The claim_revision from the current claim
        blocked_on: For BLOCKED: dependency, human, capability, or card
        blocked_referent: For BLOCKED: exact referent
        attempted: For BLOCKED: what the reviewer attempted
        pr_url: Pull request URL if applicable

    Returns:
        dict: The CardStore event that was recorded

    Raises:
        ValueError: If any validation fails
        VerdictDeliveryError: If delivery fails after a write attempt
    """
    delivery = VerdictDelivery(home)
    return delivery.record_verdict(
        review_card_id=review_card_id,
        parent_card_id=parent_card_id,
        verdict=verdict,
        evidence_uri=evidence_uri,
        evidence_sha256=evidence_sha256,
        reviewer_identity=reviewer_identity,
        claim_revision=claim_revision,
        blocked_on=blocked_on,
        blocked_referent=blocked_referent,
        attempted=attempted,
        pr_url=pr_url,
    )
