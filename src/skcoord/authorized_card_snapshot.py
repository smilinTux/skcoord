"""Policy-authorized, bounded CardStore snapshot projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .card_store import CardStore

MAX_VISIBLE_RECORDS = 2_000
MAX_VISIBLE_EDGES = 10_000
MAX_OUTPUT_RECORDS = 200
MAX_OUTPUT_FINDINGS = 200
MAX_OUTPUT_MILESTONES = 200
MAX_PATH_DEPTH = 32
MAX_PATH_REFS = 8
MAX_PROJECT_ITEM_BYTES = 384 * 1024
MAX_ID_LENGTH = 128
MAX_OWNER_LENGTH = 128
MAX_DEPENDENCIES = 128
MAX_TIMESTAMP_LENGTH = 64
STALE_AFTER = timedelta(days=30)
STALE_RULE = "dependency-unresolved-30d@1.0.0"
SEMANTIC_LABELS = frozenset(
    {
        "benefit",
        "decision",
        "human-gate",
        "investment",
        "milestone",
        "objective",
        "project",
        "risk",
    }
)
FIELD_MASK_VALUES = (
    "claim_conflict",
    "human_gate",
    "milestone",
    "orphan_evidence",
    "owner_ref",
    "stale_activity",
    "visible_edges",
)
PRIORITIES = frozenset({"low", "medium", "high", "critical"})


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorizedCardIdentityV1(_Contract):
    """Attributable principal established outside the CardStore."""

    subject_principal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    acting_principal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    node_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    capauth_identity_ref: str = Field(min_length=1, max_length=MAX_ID_LENGTH)


class AuthorizedCardScopeV1(_Contract):
    """Exact non-secret scope supported by the first reader contract."""

    role: Literal["operator", "project-manager", "architect"]
    scope: Literal["estate"] = "estate"
    service: Literal["all"] = "all"
    window: Literal["latest"] = "latest"
    baseline: Literal["none"] = "none"


class AuthorizedCardSnapshotRequestV1(_Contract):
    """Request attribution and the opaque decision reference to validate."""

    identity: AuthorizedCardIdentityV1
    scope: AuthorizedCardScopeV1
    purpose: Literal["project-management-reporting"] = "project-management-reporting"
    audience: Literal["skdashboard"] = "skdashboard"
    capability: Literal["skdashboard.read"] = "skdashboard.read"
    target: Literal["/api/v1/overview"] = "/api/v1/overview"
    resource_type: Literal["skcoord.card_store.project_snapshot"] = (
        "skcoord.card_store.project_snapshot"
    )
    resource_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    capauth_decision_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    owner_policy_revision: str = Field(min_length=1, max_length=MAX_ID_LENGTH)


class AuthorizedCardSetDecisionV1(_Contract):
    """Owner-policy result returned by the injected trusted validator."""

    capauth_decision_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    owner_policy_revision: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    state: Literal["allow", "deny", "unknown", "unavailable", "unauthorized"]
    code: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    subject_principal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    acting_principal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    node_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    capauth_identity_ref: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    purpose: Literal["project-management-reporting"] = "project-management-reporting"
    audience: Literal["skdashboard"] = "skdashboard"
    capability: Literal["skdashboard.read"] = "skdashboard.read"
    target: Literal["/api/v1/overview"] = "/api/v1/overview"
    resource_type: Literal["skcoord.card_store.project_snapshot"] = (
        "skcoord.card_store.project_snapshot"
    )
    resource_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    scope: AuthorizedCardScopeV1
    issued_at: datetime
    expires_at: datetime
    visible_card_ids: tuple[str, ...] = ()
    visible_absent_ids: tuple[str, ...] = ()
    visible_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    field_mask: tuple[
        Literal[
            "claim_conflict",
            "human_gate",
            "milestone",
            "orphan_evidence",
            "owner_ref",
            "stale_activity",
            "visible_edges",
        ],
        ...,
    ] = ()
    semantic_classes: tuple[
        Literal[
            "benefit",
            "decision",
            "human-gate",
            "investment",
            "milestone",
            "objective",
            "project",
            "risk",
        ],
        ...,
    ] = ()

    @field_validator("visible_card_ids", "visible_absent_ids")
    @classmethod
    def validate_visible_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > MAX_VISIBLE_RECORDS:
            raise ValueError("authorized card decision population exceeds the safe cap")
        if tuple(sorted(set(values))) != values:
            raise ValueError("authorized visible card ids must be sorted and unique")
        if any(_identifier(value) is None for value in values):
            raise ValueError("authorized visible card id is invalid")
        return values

    @model_validator(mode="after")
    def canonical_decision(self) -> "AuthorizedCardSetDecisionV1":
        if self.state != "allow" and self.visible_card_ids:
            raise ValueError("non-allow policy decisions cannot carry card ids")
        if self.state != "allow" and self.visible_absent_ids:
            raise ValueError("non-allow policy decisions cannot carry absent card ids")
        if (
            len(self.visible_card_ids) + len(self.visible_absent_ids)
            > MAX_VISIBLE_RECORDS
        ):
            raise ValueError("authorized card decision exceeds the combined safe cap")
        if set(self.visible_card_ids) & set(self.visible_absent_ids):
            raise ValueError("visible and attested-absent card ids must be disjoint")
        if tuple(sorted(set(self.field_mask))) != self.field_mask:
            raise ValueError("field mask must be sorted and unique")
        if tuple(sorted(set(self.semantic_classes))) != self.semantic_classes:
            raise ValueError("semantic classes must be sorted and unique")
        return self


PolicyDecisionValidator = Callable[
    [AuthorizedCardSnapshotRequestV1], AuthorizedCardSetDecisionV1
]


def _identifier(value) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_ID_LENGTH
        or not value.isascii()
    ):
        return None
    if (
        value in {".", ".."}
        or ".." in value
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _watermark(value: object) -> dict[str, str]:
    return {
        "source": "skcoord.authorized_card_snapshot",
        "value": f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}",
    }


def visible_set_sha256(values: tuple[str, ...]) -> str:
    """Canonical policy-visible identifier-set hash bound by the decision."""
    return f"sha256:{hashlib.sha256(_canonical_bytes(list(values))).hexdigest()}"


def authorized_card_resource_id(
    values: tuple[str, ...],
    field_mask: tuple[str, ...],
    semantic_classes: tuple[str, ...],
    visible_absent_ids: tuple[str, ...] = (),
    *,
    scope: AuthorizedCardScopeV1,
) -> str:
    constraint = {
        "visible_set_sha256": visible_set_sha256(values),
        "field_mask": list(field_mask),
        "semantic_classes": list(semantic_classes),
        "visible_absent_ids": list(visible_absent_ids),
        "scope": _scope(scope),
    }
    digest = hashlib.sha256(_canonical_bytes(constraint)).hexdigest()
    return f"authorized-card-set:sha256:{digest}"


def _scope(scope: AuthorizedCardScopeV1) -> dict[str, str]:
    return scope.model_dump(mode="json")


def _no_value(scope: AuthorizedCardScopeV1) -> dict:
    """Return one constant policy-unevaluated result without a source read."""
    return {
        "projection_type": "project_records",
        "schema_version": "1.0.0",
        "source_owner": "skcoord",
        "source_model": "AuthorizedCardSnapshotReader",
        "classification": "internal",
        "scope": _scope(scope),
        "visibility": {
            "state": "policy_filtered",
            "authorization": "unknown",
        },
        "truth_state": "unknown",
        "snapshot_consistency": "policy_unevaluated",
        "observed_at": None,
        "projected_at": None,
        "watermark": {"source": "skcoord.authorized_card_snapshot", "value": None},
        "population_counts": None,
        "classification_complete": False,
        "truncated": None,
        "records": [],
        "dependency_edges": [],
        "milestones": [],
        "errors": [
            {
                "code": "AUTHORIZED_SNAPSHOT_UNAVAILABLE",
                "message": "Authorized CardStore snapshot is unavailable",
                "retryable": True,
            }
        ],
    }


def unavailable_authorized_card_snapshot(scope: AuthorizedCardScopeV1) -> dict:
    """Return the stable no-value envelope without consulting a source."""
    return _no_value(scope)


def _parse_timestamp(value) -> datetime | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TIMESTAMP_LENGTH
        or not value.isascii()
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def _owner(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return (
        text
        if 0 < len(text) <= MAX_OWNER_LENGTH and len(text.encode("utf-8")) <= 256
        else None
    )


def _classes(card, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(card.labels, list):
        return ()
    approved = {
        label
        for label in card.labels
        if isinstance(label, str) and label in SEMANTIC_LABELS and label in allowed
    }
    return tuple(sorted(approved))


def _conflict(card, field_mask: frozenset[str]) -> bool:
    if "claim_conflict" not in field_mask:
        return False
    return any(
        isinstance(card.meta.get(key), list) and bool(card.meta[key])
        for key in ("claim_conflicts", "release_conflicts")
    )


def _safe_visible_dependencies(
    card, visible_ids: frozenset[str], absent_ids: frozenset[str]
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    values = card.dependencies
    if not isinstance(values, list):
        return None
    approved_visible = {
        value for value in values if isinstance(value, str) and value in visible_ids
    }
    approved_absent = {
        value for value in values if isinstance(value, str) and value in absent_ids
    }
    if len(approved_visible) + len(approved_absent) > MAX_DEPENDENCIES:
        return None
    return tuple(sorted(approved_visible)), tuple(sorted(approved_absent))


def _record(
    card,
    dependencies: tuple[str, ...],
    field_mask: frozenset[str],
    semantic_classes: frozenset[str],
) -> tuple[dict, bool]:
    priority = card.priority if card.priority in PRIORITIES else None
    owner = _owner(card.owner) if "owner_ref" in field_mask else None
    created_at = _timestamp_text(card.created_at)
    updated_at = _timestamp_text(card.updated_at)
    valid = (
        priority is not None
        and ("owner_ref" not in field_mask or card.owner is None or owner is not None)
        and (not card.created_at or created_at is not None)
        and (not card.updated_at or updated_at is not None)
        and isinstance(card.labels, list)
    )
    return (
        {
            "record_id": card.id,
            "source_ref": f"skcoord.card_store:{card.id}",
            "kind": card.kind.value,
            "classifications": list(_classes(card, semantic_classes)),
            "status": card.status.value,
            "priority": priority,
            "owner": owner,
            "created_at": created_at,
            "updated_at": updated_at,
            "archived": bool(card.archived),
            "visible_dependency_count": len(dependencies),
            "folded_conflict_evidence": _conflict(card, field_mask),
        },
        valid,
    )


def _activity(card) -> datetime | None:
    return _parse_timestamp(card.updated_at) or _parse_timestamp(card.created_at)


def _path_nodes(
    start: str, graph: dict[str, tuple[str, ...]]
) -> tuple[list[str], bool]:
    stack = [(start, 0)]
    seen: set[str] = set()
    ordered: list[str] = []
    limited = False
    while stack and len(ordered) < MAX_PATH_DEPTH:
        node, depth = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        ordered.append(node)
        if depth >= MAX_PATH_DEPTH - 1:
            limited = limited or bool(graph.get(node))
            continue
        stack.extend((child, depth + 1) for child in reversed(graph.get(node, ())))
    return ordered, limited or bool(stack)


def _reachable(
    start: str, wanted: str, graph: dict[str, tuple[str, ...]]
) -> tuple[bool, bool]:
    nodes, limited = _path_nodes(start, graph)
    return wanted in nodes, limited


class AuthorizedCardSnapshotReader:
    """Read only the identifier population authorized by an owner decision."""

    def __init__(
        self,
        home: Path,
        validate_policy_decision: PolicyDecisionValidator,
        *,
        store_factory=CardStore,
    ) -> None:
        self.home = Path(home)
        self.validate_policy_decision = validate_policy_decision
        self.store_factory = store_factory

    def read(
        self,
        request: AuthorizedCardSnapshotRequestV1,
        *,
        now: datetime | None = None,
    ) -> dict:
        denied = _no_value(request.scope)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            return denied
        current = current.astimezone(timezone.utc)
        try:
            decision = self.validate_policy_decision(request)
        except Exception:  # noqa: BLE001 - policy failure is one constant result
            return denied
        if not isinstance(decision, AuthorizedCardSetDecisionV1):
            return denied
        if not self._allows(request, decision, current):
            return denied

        try:
            store = self.store_factory(self.home)
            folded = [store.fold(card_id) for card_id in decision.visible_card_ids]
        except Exception:  # noqa: BLE001 - source failures expose no protected detail
            return denied
        return self._project(request.scope, decision, folded, current)

    @staticmethod
    def _allows(
        request: AuthorizedCardSnapshotRequestV1,
        decision: AuthorizedCardSetDecisionV1,
        now: datetime,
    ) -> bool:
        bound_values = (
            request.identity.subject_principal_id,
            request.identity.acting_principal_id,
            request.identity.node_id,
            request.identity.capauth_identity_ref,
            request.resource_id,
            request.capauth_decision_id,
            request.owner_policy_revision,
            decision.capauth_decision_id,
            decision.owner_policy_revision,
            decision.code,
            decision.subject_principal_id,
            decision.acting_principal_id,
            decision.node_id,
            decision.capauth_identity_ref,
            decision.resource_id,
        )
        return (
            decision.state == "allow"
            and decision.code == "ALLOW"
            and all(_identifier(value) is not None for value in bound_values)
            and decision.capauth_decision_id == request.capauth_decision_id
            and decision.owner_policy_revision == request.owner_policy_revision
            and decision.subject_principal_id == request.identity.subject_principal_id
            and decision.acting_principal_id == request.identity.acting_principal_id
            and decision.node_id == request.identity.node_id
            and decision.capauth_identity_ref == request.identity.capauth_identity_ref
            and decision.purpose == request.purpose
            and decision.audience == request.audience
            and decision.capability == request.capability
            and decision.target == request.target
            and decision.resource_type == request.resource_type
            and decision.resource_id == request.resource_id
            and decision.visible_set_sha256
            == visible_set_sha256(decision.visible_card_ids)
            and decision.resource_id
            == authorized_card_resource_id(
                decision.visible_card_ids,
                decision.field_mask,
                decision.semantic_classes,
                decision.visible_absent_ids,
                scope=decision.scope,
            )
            and decision.scope == request.scope
            and decision.issued_at.tzinfo is not None
            and decision.issued_at.utcoffset() is not None
            and decision.expires_at.tzinfo is not None
            and decision.expires_at.utcoffset() is not None
            and decision.issued_at.utcoffset() == timedelta(0)
            and decision.expires_at.utcoffset() == timedelta(0)
            and decision.issued_at <= now < decision.expires_at
        )

    def _project(
        self,
        scope: AuthorizedCardScopeV1,
        decision: AuthorizedCardSetDecisionV1,
        folded: list,
        now: datetime,
    ) -> dict:
        gaps = sum(card is None for card in folded)
        cards = [card for card in folded if card is not None]
        mismatches = sum(
            card.id != expected
            for expected, card in zip(decision.visible_card_ids, folded, strict=True)
            if card is not None
        )
        cards = [
            card
            for expected, card in zip(decision.visible_card_ids, folded, strict=True)
            if card is not None and card.id == expected
        ]
        visible_ids = frozenset(card.id for card in cards)
        absent_ids = frozenset(decision.visible_absent_ids)
        field_mask = frozenset(decision.field_mask)
        semantic_classes = frozenset(decision.semantic_classes)
        dependency_map = {
            card.id: (
                _safe_visible_dependencies(card, visible_ids, absent_ids)
                if "visible_edges" in field_mask
                else ((), ())
            )
            for card in cards
        }
        invalid_dependencies = sum(value is None for value in dependency_map.values())
        graph = {
            card_id: value[0] if value is not None else ()
            for card_id, value in dependency_map.items()
        }
        attested_absent = {
            card_id: (
                value[1]
                if value is not None and "orphan_evidence" in field_mask
                else ()
            )
            for card_id, value in dependency_map.items()
        }
        record_pairs = [
            _record(card, graph[card.id], field_mask, semantic_classes)
            for card in cards
        ]
        invalid_fields = sum(not valid for _record_value, valid in record_pairs)
        safe_records = [value for value, _valid in record_pairs]

        raw_visible_edges = sorted(
            (source, target) for source, targets in graph.items() for target in targets
        )
        raw_orphan_edges = sorted(
            (source, target)
            for source, targets in attested_absent.items()
            for target in targets
        )
        raw_edges = sorted(
            [(source, target, "visible") for source, target in raw_visible_edges]
            + [(source, target, "orphan") for source, target in raw_orphan_edges]
        )
        edge_cap = len(raw_edges) > MAX_VISIBLE_EDGES
        by_id = {card.id: card for card in cards}
        classified = [
            (
                self._edge(
                    source,
                    target,
                    graph,
                    by_id,
                    now,
                    field_mask,
                    semantic_classes,
                )
                if edge_type == "visible"
                else self._orphan(source, target, by_id, field_mask)
            )
            for source, target, edge_type in raw_edges[:MAX_VISIBLE_EDGES]
        ]
        classified.sort(key=self._finding_key)
        findings = classified[:MAX_OUTPUT_FINDINGS]
        exceptional = {
            record_id
            for finding in findings
            if finding["conditions"]
            for record_id in finding["evidence_refs"]
        }
        selected_ids = sorted(
            visible_ids,
            key=lambda card_id: (
                card_id not in exceptional,
                not bool(_classes(by_id[card_id], semantic_classes)),
                by_id[card_id].status.value == "done",
                card_id,
            ),
        )[:MAX_OUTPUT_RECORDS]
        records_by_id = {record["record_id"]: record for record in safe_records}
        records = [records_by_id[card_id] for card_id in selected_ids]
        milestone_cards = [
            card
            for card in cards
            if "milestone" in field_mask
            and "milestone" in _classes(card, semantic_classes)
        ]
        milestones = [
            self._milestone(card, graph, classified, records_by_id, edge_cap)
            for card in sorted(milestone_cards, key=lambda value: value.id)[
                :MAX_OUTPUT_MILESTONES
            ]
        ]
        output_truncated = (
            len(safe_records) > len(records)
            or len(classified) > len(findings)
            or len(milestone_cards) > len(milestones)
        )
        classification_complete = not (
            gaps or mismatches or invalid_dependencies or invalid_fields or edge_cap
        )
        facts = {
            "records": safe_records,
            "analyzed_edges": raw_edges[:MAX_VISIBLE_EDGES],
            "classification_complete": classification_complete,
        }
        errors = []
        if not classification_complete:
            errors.append(
                {
                    "code": "AUTHORIZED_CARD_SNAPSHOT_PARTIAL",
                    "message": "The authorized CardStore snapshot has a bounded consistency or field gap",
                    "retryable": bool(gaps or mismatches),
                }
            )
        if output_truncated:
            errors.append(
                {
                    "code": "AUTHORIZED_CARD_OUTPUT_TRUNCATED",
                    "message": "The authorized CardStore output is truncated after bounded classification",
                    "retryable": False,
                }
            )
        observed = [value for card in cards if (value := _activity(card)) is not None]
        result = {
            "projection_type": "project_records",
            "schema_version": "1.0.0",
            "source_owner": "skcoord",
            "source_model": "AuthorizedCardSnapshotReader",
            "classification": "internal",
            "scope": _scope(scope),
            "policy_decision": {
                "capauth_decision_id": decision.capauth_decision_id,
                "owner_policy_revision": decision.owner_policy_revision,
                "visible_set_sha256": decision.visible_set_sha256,
                "expires_at": decision.expires_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            "visibility": {
                "state": "visible",
                "authorization": "authorized",
                "reason": "Owner policy filtered identifiers before CardStore retrieval",
            },
            "truth_state": (
                "current"
                if classification_complete and not output_truncated
                else "partial"
            ),
            "snapshot_consistency": "per_authorized_record_fold",
            "observed_at": (
                max(observed).isoformat().replace("+00:00", "Z") if observed else None
            ),
            "projected_at": now.isoformat().replace("+00:00", "Z"),
            "watermark": _watermark(facts),
            "population_counts": {
                "authorized_ids": len(decision.visible_card_ids),
                "folded": len(cards),
                "emitted_records": len(records),
                "visible_edges": len(raw_visible_edges),
                "attested_orphan_edges": len(raw_orphan_edges),
                "emitted_findings": len(findings),
                "explicit_milestones": len(milestone_cards),
                "emitted_milestones": len(milestones),
            },
            "classification_complete": classification_complete,
            "truncated": output_truncated,
            "records": records,
            "dependency_edges": findings,
            "milestones": milestones,
            "errors": errors,
        }
        return self._fit(scope, result)

    @staticmethod
    def _edge(
        source_id: str,
        target_id: str,
        graph,
        by_id,
        now: datetime,
        field_mask: frozenset[str],
        semantic_classes: frozenset[str],
    ) -> dict:
        source = by_id[source_id]
        target = by_id[target_id]
        path_ids, limited = _path_nodes(target_id, graph)
        path_cards = [by_id[card_id] for card_id in path_ids]
        cycle, cycle_limited = _reachable(target_id, source_id, graph)
        limited = limited or cycle_limited
        unresolved = [card for card in path_cards if card.status.value != "done"]
        stale = [
            card
            for card in unresolved
            if "stale_activity" in field_mask
            if (activity := _activity(card)) is not None
            and activity <= now
            and now - activity > STALE_AFTER
        ]
        freshness_unknown = [
            card
            for card in unresolved
            if "stale_activity" in field_mask
            and ((activity := _activity(card)) is None or activity > now)
        ]
        claim_conflict = _conflict(source, field_mask) or any(
            _conflict(card, field_mask) for card in path_cards
        )
        conditions = []
        for state, present in (
            ("stale", bool(stale)),
            ("freshness_unknown", bool(freshness_unknown)),
            ("conflicted", cycle or claim_conflict),
            ("dependency_cycle", cycle),
            ("record_claim_conflict", claim_conflict),
            (
                "human_gated",
                any(
                    "human_gate" in field_mask
                    and card.status.value != "done"
                    and "human-gate" in _classes(card, semantic_classes)
                    for card in [source, *path_cards]
                ),
            ),
            (
                "milestone_path",
                "milestone" in field_mask
                and any(
                    "milestone" in _classes(card, semantic_classes)
                    for card in [source, *path_cards]
                ),
            ),
            ("archived_target", bool(target.archived)),
            ("path_classification_partial", limited),
        ):
            if present:
                conditions.append(state)
        return {
            "from_record_id": source_id,
            "to_record_id": target_id,
            "resolution": "satisfied" if target.status.value == "done" else "open",
            "conditions": conditions,
            "source_owner": _owner(source.owner) if "owner_ref" in field_mask else None,
            "target_owner": _owner(target.owner) if "owner_ref" in field_mask else None,
            "target_status": target.status.value,
            "stale_rule": STALE_RULE,
            "stale_record_refs": [card.id for card in stale[:MAX_PATH_REFS]],
            "freshness_unknown_record_refs": [
                card.id for card in freshness_unknown[:MAX_PATH_REFS]
            ],
            "path_record_ids": path_ids[:MAX_PATH_REFS],
            "evidence_refs": [source_id, *path_ids[: MAX_PATH_REFS - 1]],
        }

    @staticmethod
    def _orphan(
        source_id: str, target_id: str, by_id, field_mask: frozenset[str]
    ) -> dict:
        source = by_id[source_id]
        return {
            "from_record_id": source_id,
            "to_record_id": target_id,
            "resolution": "orphaned",
            "conditions": ["owner_attested_absent"],
            "source_owner": _owner(source.owner) if "owner_ref" in field_mask else None,
            "target_owner": None,
            "target_status": None,
            "stale_rule": None,
            "stale_record_refs": [],
            "freshness_unknown_record_refs": [],
            "path_record_ids": [],
            "evidence_refs": [source_id, target_id],
        }

    @staticmethod
    def _finding_key(finding: dict) -> tuple:
        if finding["resolution"] == "orphaned":
            return -1, finding["from_record_id"], finding["to_record_id"]
        conditions = set(finding["conditions"])
        order = (
            "dependency_cycle",
            "human_gated",
            "record_claim_conflict",
            "stale",
            "freshness_unknown",
            "archived_target",
            "path_classification_partial",
        )
        rank = next(
            (index for index, value in enumerate(order) if value in conditions), 9
        )
        return rank, finding["from_record_id"], finding["to_record_id"]

    @staticmethod
    def _milestone(card, graph, classified, records_by_id, partial: bool) -> dict:
        nodes, limited = _path_nodes(card.id, graph)
        node_set = set(nodes)
        paths = [
            finding for finding in classified if finding["from_record_id"] in node_set
        ]
        conditions = {
            condition: sum(condition in finding["conditions"] for finding in paths)
            for condition in (
                "stale",
                "freshness_unknown",
                "conflicted",
                "dependency_cycle",
                "record_claim_conflict",
                "human_gated",
                "owner_attested_absent",
                "archived_target",
                "path_classification_partial",
            )
        }
        return {
            **records_by_id[card.id],
            "dependency_path_summary": {
                "findings": len(paths),
                "conditions": conditions,
                "path_record_ids": nodes[:MAX_PATH_REFS],
                "partial": partial or limited,
            },
        }

    @staticmethod
    def _fit(scope: AuthorizedCardScopeV1, result: dict) -> dict:
        return (
            result
            if len(_canonical_bytes(result)) <= MAX_PROJECT_ITEM_BYTES
            else _no_value(scope)
        )
