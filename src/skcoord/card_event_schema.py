"""Warning-tier validation for append-only CardStore events.

``card-event.v1`` is additive.  Writers emit the version and provenance fields,
while readers continue to accept pre-v1 history and report its omissions as
warnings.  Structural lifecycle events and evidence events remain distinct:
only an explicit evidence event may contribute a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CARD_EVENT_SCHEMA_VERSION = "card-event.v1"
PROVENANCE_KEYS = ("host", "agent_id", "harness")
REQUIRED_EVENT_KEYS = ("event_id", "ts", "writer", "seq", "action")
EVIDENCE_ACTION = "evidence"


@dataclass(frozen=True)
class CardEventFinding:
    """One machine-readable schema finding."""

    level: str
    code: str
    field: str
    message: str


def validate_card_event(
    event: Mapping[str, Any], *, historical: bool = True
) -> tuple[CardEventFinding, ...]:
    """Validate one event without making historical records unreadable.

    Missing v1/provenance fields are warnings for historical reads and errors
    at the new-write boundary.  Malformed fields explicitly claiming v1 are
    always errors.
    """
    findings: list[CardEventFinding] = []
    missing_level = "warning" if historical else "error"

    for key in REQUIRED_EVENT_KEYS:
        if key not in event:
            findings.append(
                CardEventFinding("error", "missing-structural-field", key, f"missing {key}")
            )

    version = event.get("schema_version")
    if version is None:
        findings.append(
            CardEventFinding(
                missing_level,
                "legacy-schema-version",
                "schema_version",
                f"missing {CARD_EVENT_SCHEMA_VERSION} marker",
            )
        )
    elif version != CARD_EVENT_SCHEMA_VERSION:
        findings.append(
            CardEventFinding(
                "error",
                "unsupported-schema-version",
                "schema_version",
                f"unsupported card event schema {version!r}",
            )
        )

    provenance = event.get("provenance")
    if not isinstance(provenance, Mapping):
        findings.append(
            CardEventFinding(
                missing_level,
                "missing-provenance",
                "provenance",
                "missing card event provenance",
            )
        )
    else:
        for key in PROVENANCE_KEYS:
            value = provenance.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    CardEventFinding(
                        "error" if version == CARD_EVENT_SCHEMA_VERSION else missing_level,
                        "invalid-provenance-field",
                        f"provenance.{key}",
                        f"provenance.{key} must be a non-empty string",
                    )
                )

    if event.get("action") == EVIDENCE_ACTION:
        for key in ("subject_event_id", "verdict"):
            value = event.get(key)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    CardEventFinding(
                        "error",
                        "invalid-evidence-event",
                        key,
                        f"evidence event {key} must be a non-empty string",
                    )
                )
    return tuple(findings)


def join_event_evidence(
    structural_events: Iterable[Mapping[str, Any]],
    evidence_events: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[dict, ...]]:
    """Join CardStore structure to separate coordination evidence by event id.

    The two arguments are deliberately separate trust domains: callers read
    structure from ``cards/<id>/events/*.jsonl`` and evidence from
    ``coordination/card_events/*.jsonl``.  Only an explicit ``evidence`` event
    with ``subject_event_id`` and ``verdict`` joins.  Lifecycle, completion,
    and link events in either source never synthesize a verdict.  Orphan
    evidence remains readable in its source stream but is absent here.
    """
    structural = [dict(event) for event in structural_events]
    evidence = [dict(event) for event in evidence_events]
    structural_ids = {
        event.get("event_id")
        for event in structural
        if isinstance(event.get("event_id"), str) and event.get("event_id")
    }
    joined: dict[str, list[dict]] = {event_id: [] for event_id in structural_ids}
    for event in evidence:
        if event.get("action") != EVIDENCE_ACTION:
            continue
        subject = event.get("subject_event_id")
        verdict = event.get("verdict")
        if subject in structural_ids and isinstance(verdict, str) and verdict.strip():
            joined[subject].append(event)
    return {key: tuple(value) for key, value in joined.items()}
