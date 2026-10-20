"""Claim-bound, transactional review result recording.

A review result is one CardStore event. The event contains both the evidence
and terminal state, so consumers never have to infer a verdict from links,
mail, or lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .card_store import CardStore, card_mutation_lock

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALID_VERDICTS = {"PASS", "BLOCKED"}
_BLOCKED_REFERENTS = {
    "dependency": re.compile(r"^card:[A-Za-z0-9._-]+$"),
    "human": re.compile(r"^approval:[A-Za-z0-9._:-]+$"),
    "capability": re.compile(r"^(?:ac:[1-9][0-9]*|free:[^\n]+)$"),
    "card": re.compile(r"^ac:[1-9][0-9]*$"),
}


@dataclass(frozen=True)
class ReviewResultReceipt:
    """Durable result and best-effort notification disposition."""

    event_id: str
    transition_id: str
    replayed: bool
    notification_errors: tuple[str, ...]


def _local_evidence_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("evidence URI must name a local file")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise ValueError("evidence URI must contain an absolute path")
    return path


def _parent_labels(labels: list[str]) -> list[str]:
    return [value for value in labels if value.startswith("parent-")]


def validate_review_result_event(card: Any, event: dict[str, Any]) -> str | None:
    """Return a reason when a raw result cannot authoritatively terminal-fold."""
    required = {
        "version",
        "review_card_id",
        "parent_card_id",
        "reviewer_identity",
        "claim_revision",
        "verdict",
        "evidence_uri",
        "evidence_sha256",
        "blocked_on",
        "blocked_referent",
        "transition_id",
        "writer",
        "event_id",
    }
    if set(event) < required:
        return "missing required review result field"
    if event.get("version") != 1:
        return "unsupported review result version"
    if event.get("review_card_id") != card.id:
        return "review card identity mismatch"
    labels = _parent_labels(card.labels)
    expected_label = f"parent-{event.get('parent_card_id')}"
    if labels != [expected_label]:
        return "parent binding mismatch"
    reviewer = event.get("reviewer_identity")
    if not isinstance(reviewer, str) or not reviewer:
        return "missing reviewer identity"
    if event.get("writer") != reviewer or card.owner != reviewer:
        return "reviewer claim mismatch"
    revision = event.get("claim_revision")
    if not isinstance(revision, str) or not revision:
        return "missing claim revision"
    if card.meta.get("_claim_revision") != revision:
        return "claim revision mismatch"
    transition = event.get("transition_id")
    if transition != f"review-result:{card.id}:{revision}":
        return "transition identity mismatch"
    verdict = event.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return "invalid review verdict"
    digest = event.get("evidence_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        return "invalid evidence digest"
    try:
        _local_evidence_path(event.get("evidence_uri"))
    except (TypeError, ValueError):
        return "invalid evidence URI"
    blocked_on = event.get("blocked_on")
    referent = event.get("blocked_referent")
    if verdict == "PASS" and (blocked_on is not None or referent is not None):
        return "PASS cannot carry blocked fields"
    if verdict == "BLOCKED":
        pattern = _BLOCKED_REFERENTS.get(blocked_on)
        if pattern is None or not isinstance(referent, str) or pattern.fullmatch(referent) is None:
            return "BLOCKED requires an exact category referent"
    return None


def _validate_evidence(uri: str, expected_sha256: str) -> None:
    path = _local_evidence_path(uri)
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"evidence file does not exist: {path}") from exc
    if path.is_symlink() or not path.is_file() or stat_result.st_nlink != 1:
        raise ValueError("evidence must be a regular single-link file")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"evidence SHA256 mismatch: expected {expected_sha256}, got {actual}")


def _canonical_payload(
    review_card_id: str,
    parent_card_id: str,
    reviewer_identity: str,
    claim_revision: str,
    verdict: str,
    evidence_uri: str,
    evidence_sha256: str,
    blocked_on: str | None,
    blocked_referent: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "review_card_id": review_card_id,
        "parent_card_id": parent_card_id,
        "reviewer_identity": reviewer_identity,
        "claim_revision": claim_revision,
        "verdict": verdict,
        "evidence_uri": evidence_uri,
        "evidence_sha256": evidence_sha256,
        "blocked_on": blocked_on,
        "blocked_referent": blocked_referent,
        "transition_id": f"review-result:{review_card_id}:{claim_revision}",
    }


def _same_result(event: dict[str, Any], payload: dict[str, Any]) -> bool:
    return all(event.get(key) == value for key, value in payload.items())


def _notify(
    reviewer: str,
    recipient: str,
    payload: dict[str, Any],
    runner: Callable[..., Any],
) -> None:
    subject = f"{payload['verdict']} review {payload['review_card_id']}"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    runner(
        ["skmail", "send", reviewer, recipient, "normal", subject, body],
        check=True,
        capture_output=True,
        text=True,
    )


def record_review_result(
    home: Path,
    *,
    review_card_id: str,
    parent_card_id: str,
    reviewer_identity: str,
    claim_revision: str,
    verdict: str,
    evidence_uri: str,
    evidence_sha256: str,
    blocked_on: str | None = None,
    blocked_referent: str | None = None,
    notify: bool = True,
    notification_runner: Callable[..., Any] = subprocess.run,
) -> ReviewResultReceipt:
    """Validate and atomically append one terminal review result.

    Notification is attempted only after exact durable readback. Notification
    errors are returned but never alter the authoritative CardStore result.
    """
    verdict = verdict.strip().upper()
    evidence_sha256 = evidence_sha256.strip().lower()
    payload = _canonical_payload(
        review_card_id,
        parent_card_id,
        reviewer_identity,
        claim_revision,
        verdict,
        evidence_uri,
        evidence_sha256,
        blocked_on,
        blocked_referent,
    )
    if verdict not in _VALID_VERDICTS:
        raise ValueError("verdict must be PASS or BLOCKED")
    if _SHA256.fullmatch(evidence_sha256) is None:
        raise ValueError("evidence_sha256 must be 64 lowercase hexadecimal characters")
    _validate_evidence(evidence_uri, evidence_sha256)

    store = CardStore(Path(home))
    replayed = False
    with card_mutation_lock(Path(home), review_card_id):
        review = store.fold(review_card_id)
        parent = store.fold(parent_card_id)
        if review is None:
            raise ValueError(f"review card {review_card_id} does not exist")
        if parent is None:
            raise ValueError(f"parent card {parent_card_id} does not exist")
        labels = _parent_labels(review.labels)
        if labels != [f"parent-{parent_card_id}"]:
            raise ValueError("review card must have exactly one matching parent label")
        if reviewer_identity in {parent.originator, parent.owner}:
            raise ValueError("reviewer identity must be distinct from the parent producer")

        existing = next(
            (
                event
                for event in store._read_events(review_card_id)
                if event.get("transition_id") == payload["transition_id"]
            ),
            None,
        )
        if existing is not None:
            if not _same_result(existing, payload):
                raise ValueError("claim revision already has a different review result")
            event = existing
            replayed = True
        else:
            if review.owner != reviewer_identity:
                raise ValueError("review card is not claimed by reviewer identity")
            if review.meta.get("_claim_revision") != claim_revision:
                raise ValueError("stale claim revision")
            candidate = {**payload, "writer": reviewer_identity, "event_id": "pending"}
            reason = validate_review_result_event(review, candidate)
            if reason is not None:
                raise ValueError(reason)
            event = store.append_event(
                review_card_id,
                "review_result",
                reviewer_identity,
                **payload,
            )

    durable = next(
        (
            value
            for value in store._read_events(review_card_id)
            if value.get("event_id") == event.get("event_id")
        ),
        None,
    )
    folded = store.fold(review_card_id)
    if durable is None or folded is None or folded.meta.get("review_result") != durable:
        raise RuntimeError("review result durable readback failed")
    if folded.status.value != "done" or folded.owner is not None:
        raise RuntimeError("review result terminal readback failed")

    errors: list[str] = []
    if notify:
        for recipient in ("jarvis", "lumina"):
            try:
                _notify(reviewer_identity, recipient, payload, notification_runner)
            except Exception as exc:  # notification is deliberately non-authoritative
                errors.append(f"{recipient}: {exc}")
    return ReviewResultReceipt(
        event_id=str(event["event_id"]),
        transition_id=payload["transition_id"],
        replayed=replayed,
        notification_errors=tuple(errors),
    )
