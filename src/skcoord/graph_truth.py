"""Deterministic joined coordination truth and read-only graph audits."""

from __future__ import annotations

import hashlib
import re
import socket
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .card import CardEvent, CardEventLog
from .card_store import CardStore

_FENCE_KEY_WORDS = re.compile(
    r"(?i)(?:^|[-_])(?:dependenc\w*|block\w*|cycle|gate|fence|locked)(?:$|[-_])"
)
_FENCE_VALUE_WORDS = re.compile(
    r"(?i)\b(?:blocked|blocking|depends?|dependency|cycle|fence|locked)\b"
    r"|\bhuman[ -]gate\b|\brequired before\b|\bmust not (?:execute|claim)\b"
    r"|\bdo not (?:execute|claim)\b"
)
_DEPENDENCY_WORDS = re.compile(r"(?i)\b(?:depends?|dependency|cycle)\b")
_CLAIM_WORDS = re.compile(r"(?i)\b(?:active[ -]?claim|claimed|do not claim|must not claim)\b")
_VOID_WORDS = re.compile(r"(?i)\bvoid(?:ed)?\b")
_ARCHIVE_WORDS = re.compile(r"(?i)\barchiv(?:e|ed|al)\b")
_CARD_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", re.IGNORECASE)
_VERDICT_TOKEN = re.compile(
    r"(?<![A-Za-z])(?:CHANGES[._ -]?REQUIRED|FAIL[._ -]?CLOSED|BLOCKED|PASS|FAIL|HOLD)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)
_VERDICT_KEYS = frozenset(
    {
        "closure-state",
        "disposition",
        "final-state",
        "gate-status",
        "independent-review",
        "result",
        "review",
        "review-result",
        "review-status",
    }
)
_ENFORCING_LABELS = frozenset({"do-not-claim", "human-gate", "not-claimable", "superseded"})


class Annotation(BaseModel):
    """One current link annotation with storage provenance."""

    key: str
    value: str = ""
    authoritative: bool = False
    legacy: bool = False


class LabelProvenance(BaseModel):
    """Current presence or explicit removal of one label in each store."""

    label: str
    authoritative: bool = False
    legacy: bool = False
    authoritative_removed: bool = False
    legacy_removed: bool = False


class JoinedCardTruth(BaseModel):
    """Current card facts joined without converting annotations into structure."""

    card_id: str
    lifecycle: str
    dependencies: list[str] = Field(default_factory=list)
    claim: str | None = None
    void: bool = False
    archived: bool = False
    labels: list[str] = Field(default_factory=list)
    label_provenance: list[LabelProvenance] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    verdicts: list[Annotation] = Field(default_factory=list)
    hashes: list[Annotation] = Field(default_factory=list)
    gate_status: list[Annotation] = Field(default_factory=list)
    review_results: list[Annotation] = Field(default_factory=list)
    supersession: list[Annotation] = Field(default_factory=list)


class AuditFinding(BaseModel):
    """One deterministic joined-truth audit finding."""

    card_id: str
    code: str
    key: str
    value: str
    referenced_cards: list[str] = Field(default_factory=list)


class GraphTruthAudit(BaseModel):
    """Bounded findings plus complete measured population counts."""

    scanned_cards: int
    total_findings: int
    truncated: bool
    population_counts: dict[str, int]
    findings: list[AuditFinding] = Field(default_factory=list)


def _normalized_key(key: str) -> str:
    """Normalize legacy key separators without changing its meaning."""
    return key.lower().replace("_", "-")


def _has_explicit_verdict(value: str) -> bool:
    """Accept canonical verdict values, not lifecycle or vague prose."""
    stripped = value.strip()
    normalized = stripped.upper().replace("-", "_").replace(".", "_").replace(" ", "_")
    exact = normalized in {
        "PASS",
        "FAIL",
        "BLOCKED",
        "HOLD",
        "FAIL_CLOSED",
        "CHANGES_REQUIRED",
    }
    return exact or bool(_VERDICT_TOKEN.search(stripped))


def _is_verdict(key: str, value: str) -> bool:
    """Return whether a recognized key carries an explicit verdict token."""
    lowered = _normalized_key(key)
    recognized = lowered in _VERDICT_KEYS or "verdict" in lowered
    return recognized and _has_explicit_verdict(value)


def _classify(annotations: list[Annotation], *needles: str) -> list[Annotation]:
    """Return annotations whose normalized key contains any ``needle``."""
    return [
        item
        for item in annotations
        if any(needle in _normalized_key(item.key) for needle in needles)
    ]


def _event_order(event: CardEvent | dict[str, Any]) -> tuple[str, str, int]:
    """Return the global legacy/store fold order for either event shape."""
    if isinstance(event, CardEvent):
        return event.ts, event.writer, event.seq
    seq = event.get("seq", 0)
    return (
        str(event.get("ts", "")),
        str(event.get("writer", "")),
        seq if isinstance(seq, int) else 0,
    )


def _current_links(events: list[CardEvent] | list[dict[str, Any]]) -> dict[str, str]:
    """Fold current links in global event order, independent of file order."""
    links: dict[str, str] = {}
    for raw in sorted(events, key=_event_order):
        event = raw.model_dump() if isinstance(raw, CardEvent) else raw
        if event.get("action") == "link" and isinstance(event.get("link_key"), str):
            links[event["link_key"]] = str(event.get("link_value") or "")
    return links


def _fold_labels(
    events: list[CardEvent] | list[dict[str, Any]], initial: list[str] | None = None
) -> dict[str, tuple[bool, bool]]:
    """Fold labels to ``label -> (present, explicitly_removed)``."""
    state = {label: (True, False) for label in (initial or [])}
    for raw in sorted(events, key=_event_order):
        event = raw.model_dump() if isinstance(raw, CardEvent) else raw
        label = event.get("label")
        if not isinstance(label, str) or not label:
            continue
        if event.get("action") == "add_label":
            state[label] = (True, False)
        elif event.get("action") == "remove_label":
            state[label] = (False, True)
    return state


def _read_joined_truth(
    store: CardStore, card_id: str, legacy_models: list[CardEvent]
) -> JoinedCardTruth:
    """Join one card using caller-cached authoritative and overlay readers."""
    store._legacy_cache = {}
    card = store.fold(card_id)
    if card is None:
        raise ValueError(f"CardStore card {card_id} has no foldable core")
    core = store._load_core(card_id) or {}
    authoritative = store._read_events(card_id)
    authoritative_links = _current_links(authoritative)
    legacy_links = _current_links(legacy_models)
    annotation_pairs = {
        (key, value)
        for links in (authoritative_links, legacy_links)
        for key, value in links.items()
    }
    annotations = [
        Annotation(
            key=key,
            value=value,
            authoritative=authoritative_links.get(key) == value,
            legacy=legacy_links.get(key) == value,
        )
        for key, value in sorted(annotation_pairs)
    ]
    authoritative_labels = _fold_labels(authoritative, list(core.get("initial_labels", [])))
    legacy_labels = _fold_labels(legacy_models)
    label_provenance = [
        LabelProvenance(
            label=label,
            authoritative=authoritative_labels.get(label, (False, False))[0],
            legacy=legacy_labels.get(label, (False, False))[0],
            authoritative_removed=authoritative_labels.get(label, (False, False))[1],
            legacy_removed=legacy_labels.get(label, (False, False))[1],
        )
        for label in sorted(set(authoritative_labels) | set(legacy_labels))
    ]
    return JoinedCardTruth(
        card_id=card_id,
        lifecycle=card.status.value,
        dependencies=sorted(card.dependencies),
        claim=card.owner,
        void=any(event.get("action") == "void" for event in authoritative),
        archived=card.archived,
        labels=[item.label for item in label_provenance if item.authoritative or item.legacy],
        label_provenance=label_provenance,
        annotations=annotations,
        verdicts=[item for item in annotations if _is_verdict(item.key, item.value)],
        hashes=_classify(annotations, "sha256", "hash"),
        gate_status=_classify(annotations, "gate"),
        review_results=[
            item
            for item in annotations
            if "review" in _normalized_key(item.key) and _is_verdict(item.key, item.value)
        ],
        supersession=_classify(annotations, "supersed"),
    )


def read_joined_truth(home: Path, card_id: str) -> JoinedCardTruth:
    """Read structural CardStore facts and legacy annotation evidence together."""
    home = Path(home).expanduser()
    store = CardStore(home)
    legacy_models = [event for event in CardEventLog(home).read_all() if event.card_id == card_id]
    return _read_joined_truth(store, card_id, legacy_models)


def _annotation_text(annotation: Annotation) -> str:
    return f"{annotation.key} {annotation.value}"


def _mentioned_labels(annotation: Annotation) -> set[str]:
    """Return exact recognized enforcing labels named by this annotation."""
    normalized = re.sub(r"[_ ]+", "-", _annotation_text(annotation).lower())
    return {
        label
        for label in _ENFORCING_LABELS
        if re.search(rf"(?<![a-z0-9]){re.escape(label)}(?![a-z0-9])", normalized)
    }


def _has_enforced_mechanism(truth: JoinedCardTruth, annotation: Annotation) -> bool:
    """Return whether this annotation names a matching current mechanism."""
    text = _annotation_text(annotation)
    references = set(_CARD_ID.findall(text))
    if references:
        return references.issubset(set(truth.dependencies))

    labels = _mentioned_labels(annotation)
    current_labels = {label.lower() for label in truth.labels}
    if labels and labels.issubset(current_labels):
        return True
    if _CLAIM_WORDS.search(text) and truth.claim:
        return True
    if _VOID_WORDS.search(text) and truth.void:
        return True
    if _ARCHIVE_WORDS.search(text) and truth.archived:
        return True
    # A dependency assertion without a target cannot be legitimized by some
    # unrelated edge on the card.
    if _DEPENDENCY_WORDS.search(text):
        return False
    return False


def _asserts_fence(annotation: Annotation) -> bool:
    """Return whether annotation text asserts, rather than merely names, a fence."""
    return bool(
        _FENCE_KEY_WORDS.search(annotation.key) or _FENCE_VALUE_WORDS.search(annotation.value)
    )


def audit_graph_truth(home: Path, limit: int = 200) -> GraphTruthAudit:
    """Read all authoritative cards and report bounded two-store truth gaps."""
    if not 1 <= limit <= 2000:
        raise ValueError("audit limit must be between 1 and 2000")
    home = Path(home).expanduser()
    store = CardStore(home)
    card_ids = store.list_card_ids()
    known_cards = set(card_ids)
    legacy_by_card: dict[str, list[CardEvent]] = {}
    for event in CardEventLog(home).read_all():
        legacy_by_card.setdefault(event.card_id, []).append(event)
    findings: list[AuditFinding] = []
    counts = {
        "unenforced_annotations": 0,
        "legacy_only_verdict_evidence": 0,
        "authoritative_only_verdict_evidence": 0,
    }
    for card_id in card_ids:
        truth = _read_joined_truth(store, card_id, legacy_by_card.get(card_id, []))
        for annotation in truth.annotations:
            if _asserts_fence(annotation) and not _has_enforced_mechanism(truth, annotation):
                counts["unenforced_annotations"] += 1
                findings.append(
                    AuditFinding(
                        card_id=card_id,
                        code="unenforced_annotation",
                        key=annotation.key,
                        value=annotation.value,
                        referenced_cards=sorted(
                            known_cards.intersection(
                                _CARD_ID.findall(_annotation_text(annotation))
                            )
                        ),
                    )
                )
            if _is_verdict(annotation.key, annotation.value):
                if annotation.legacy and not annotation.authoritative:
                    counts["legacy_only_verdict_evidence"] += 1
                    code = "legacy_only_verdict_evidence"
                elif annotation.authoritative and not annotation.legacy:
                    counts["authoritative_only_verdict_evidence"] += 1
                    code = "authoritative_only_verdict_evidence"
                else:
                    continue
                findings.append(
                    AuditFinding(
                        card_id=card_id,
                        code=code,
                        key=annotation.key,
                        value=annotation.value,
                    )
                )
    findings.sort(key=lambda item: (item.card_id, item.code, item.key, item.value))
    return GraphTruthAudit(
        scanned_cards=len(card_ids),
        total_findings=len(findings),
        truncated=len(findings) > limit,
        population_counts=counts,
        findings=findings[:limit],
    )


def _matches(event: dict[str, Any], action: str, payload: dict[str, str]) -> bool:
    """Return whether a raw event exactly carries an intended annotation."""
    return event.get("action") == action and all(
        event.get(key) == value for key, value in payload.items()
    )


def _fold_reflects(card, action: str, payload: dict[str, str]) -> bool:
    """Return whether a fresh fold applies one supported annotation action."""
    if action == "link":
        return card.links.get(payload["link_key"]) == payload["link_value"]
    if action == "add_label":
        return payload["label"] in card.labels
    return payload["label"] not in card.labels


def _exact_overlay_event(
    event: CardEvent,
    *,
    event_id: str,
    card_id: str,
    action: str,
    writer: str,
    payload: dict[str, str],
) -> bool:
    raw = event.model_dump()
    return (
        raw.get("event_id") == event_id
        and raw.get("card_id") == card_id
        and raw.get("writer") == writer
        and _matches(raw, action, payload)
    )


def write_verified_annotation(
    home: Path,
    card_id: str,
    action: str,
    writer: str = "",
    *,
    label: str | None = None,
    link_key: str | None = None,
    link_value: str | None = None,
    transition_id: str | None = None,
) -> dict[str, Any]:
    """Write and independently verify one exact two-store annotation pair."""
    if action not in {"add_label", "remove_label", "link"}:
        raise ValueError("annotation action must be add_label, remove_label, or link")
    payload = (
        {"label": label} if action != "link" else {"link_key": link_key, "link_value": link_value}
    )
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise ValueError("annotation payload values are required")
    if transition_id is not None and (not isinstance(transition_id, str) or not transition_id):
        raise ValueError("transition_id must be a non-empty string")
    home = Path(home).expanduser()
    writer = writer or socket.gethostname()
    operation_id = transition_id or uuid.uuid4().hex

    # Import through the module so tests and integrations observing the
    # existing lock entry point see the exact critical section used here.
    from . import card_store as card_store_module

    # Reject unknown cards before lock creation. append_event repeats this
    # validation inside the lock to defend against a raced card-store change.
    store = CardStore(home)
    store._require_foldable_core(card_id)
    lock_filename = f"{hashlib.sha256(card_id.encode('utf-8')).hexdigest()}.lock"
    existing_anchor = card_store_module._open_existing_coordination_lock(
        home, lock_filename, "card"
    )
    if existing_anchor is None:
        # A valid card must receive its anchor during create. Refuse a partial
        # or externally assembled card rather than manufacture coordination
        # state in the artifact-neutral helper path.
        raise ValueError(f"CardStore card {card_id} has no stable lock anchor")
    existing_anchor.close()
    with card_store_module.card_mutation_lock(home, card_id, artifact_neutral=True):
        event = CardStore(home).append_event(
            card_id,
            action,
            writer,
            transition_id=operation_id,
            **payload,
        )
        event_id = event.get("event_id")
        reader = CardStore(home)
        authoritative = reader._read_events(card_id)
        folded = reader.fold(card_id)
        exact_authoritative = [
            item
            for item in authoritative
            if item.get("event_id") == event_id
            and item.get("writer") == writer
            and item.get("transition_id") == operation_id
            and _matches(item, action, payload)
        ]
        if (
            not isinstance(event_id, str)
            or len(exact_authoritative) != 1
            or folded is None
            or not _fold_reflects(folded, action, payload)
        ):
            raise RuntimeError(f"authoritative readback failed for {card_id}; no success reported")
        try:
            existing = [
                item for item in CardEventLog(home).read_all() if item.event_id == event_id
            ]
            if existing and (
                len(existing) != 1
                or not _exact_overlay_event(
                    existing[0],
                    event_id=event_id,
                    card_id=card_id,
                    action=action,
                    writer=writer,
                    payload=payload,
                )
            ):
                raise RuntimeError("legacy overlay event identity conflict")
            if not existing:
                CardEventLog(home).append(
                    CardEvent(
                        event_id=event_id,
                        card_id=card_id,
                        action=action,
                        writer=writer,
                        **payload,
                    )
                )
            readback = [
                item for item in CardEventLog(home).read_all() if item.event_id == event_id
            ]
            if len(readback) != 1 or not _exact_overlay_event(
                readback[0],
                event_id=event_id,
                card_id=card_id,
                action=action,
                writer=writer,
                payload=payload,
            ):
                raise RuntimeError("legacy overlay readback failed")
        except Exception as exc:
            raise RuntimeError(
                f"authoritative event {event_id} is durable but legacy overlay failed; "
                "partial state reported"
            ) from exc
    return event
