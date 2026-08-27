from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from skcoord.authorized_card_snapshot import (
    MAX_PROJECT_ITEM_BYTES,
    AuthorizedCardIdentityV1,
    AuthorizedCardScopeV1,
    AuthorizedCardSetDecisionV1,
    AuthorizedCardSnapshotReader,
    AuthorizedCardSnapshotRequestV1,
    authorized_card_resource_id,
    visible_set_sha256,
)
from skcoord.card import Card, Column, Kind
from skcoord.card_store import CardCore, CardStore

NOW = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
FIELD_MASK = (
    "claim_conflict",
    "human_gate",
    "milestone",
    "orphan_evidence",
    "owner_ref",
    "stale_activity",
    "visible_edges",
)
SEMANTIC_CLASSES = (
    "benefit",
    "decision",
    "human-gate",
    "investment",
    "milestone",
    "objective",
    "project",
    "risk",
)
SCOPE = AuthorizedCardScopeV1(role="project-manager")
IDENTITY = AuthorizedCardIdentityV1(
    subject_principal_id="human@example.test",
    acting_principal_id="skdashboard-service",
    node_id="chiap04",
    capauth_identity_ref="capauth-identity-01",
)


def _request(
    visible=(),
    field_mask=FIELD_MASK,
    semantic_classes=SEMANTIC_CLASSES,
    visible_absent=(),
    **changes,
):
    visible = tuple(sorted(visible))
    visible_absent = tuple(sorted(visible_absent))
    values = {
        "identity": IDENTITY,
        "scope": SCOPE,
        "resource_id": authorized_card_resource_id(
            visible, field_mask, semantic_classes, visible_absent, scope=SCOPE
        ),
        "capauth_decision_id": "capauth-decision-01",
        "owner_policy_revision": "owner-policy-r1",
    }
    values.update(changes)
    return AuthorizedCardSnapshotRequestV1(**values)


REQUEST = _request()


def _decision(*, state="allow", visible=(), visible_absent=(), **changes):
    visible = tuple(sorted(visible))
    visible_absent = tuple(sorted(visible_absent))
    values = {
        "capauth_decision_id": "capauth-decision-01",
        "owner_policy_revision": "owner-policy-r1",
        "state": state,
        "code": "ALLOW" if state == "allow" else "DENY",
        "subject_principal_id": IDENTITY.subject_principal_id,
        "acting_principal_id": IDENTITY.acting_principal_id,
        "node_id": IDENTITY.node_id,
        "capauth_identity_ref": IDENTITY.capauth_identity_ref,
        "resource_id": authorized_card_resource_id(
            visible, FIELD_MASK, SEMANTIC_CLASSES, visible_absent, scope=SCOPE
        ),
        "scope": SCOPE,
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=5),
        "visible_card_ids": visible,
        "visible_absent_ids": visible_absent,
        "visible_set_sha256": visible_set_sha256(visible),
        "field_mask": FIELD_MASK,
        "semantic_classes": SEMANTIC_CLASSES,
    }
    values.update(changes)
    return AuthorizedCardSetDecisionV1(**values)


def _card(
    card_id: str,
    *,
    dependencies=(),
    labels=(),
    status=Column.BACKLOG,
    archived=False,
    owner="record-owner",
    priority="medium",
    created_at="2026-08-01T00:00:00Z",
    updated_at="2026-08-20T00:00:00Z",
    meta=None,
):
    return Card(
        id=card_id,
        kind=Kind.TASK,
        title="SECRET-TITLE",
        description="SECRET-DESCRIPTION",
        status=status,
        swimlane="feature",
        priority=priority,
        originator="SECRET-ORIGINATOR",
        owner=owner,
        labels=list(labels),
        acceptance_criteria=["SECRET-CRITERIA"],
        dependencies=list(dependencies),
        links={"capability": "SECRET-CAPABILITY"},
        meta=dict(meta or {}),
        archived=archived,
        created_at=created_at,
        updated_at=updated_at,
    )


class _Store:
    def __init__(self, cards):
        self.cards = {card.id: card for card in cards}
        self.fold_calls = []
        self.list_card_ids = Mock(
            side_effect=AssertionError("raw id enumeration called")
        )
        self.list_cards = Mock(
            side_effect=AssertionError("raw record enumeration called")
        )
        self.create = Mock(side_effect=AssertionError("mutation called"))
        self.append_event = Mock(side_effect=AssertionError("mutation called"))

    def fold(self, card_id):
        self.fold_calls.append(card_id)
        return self.cards.get(card_id)


def _read(cards, decision, *, request=None, now=NOW):
    request = request or _request(
        decision.visible_card_ids,
        decision.field_mask,
        decision.semantic_classes,
        decision.visible_absent_ids,
    )
    store = _Store(cards)
    factory = Mock(return_value=store)
    reader = AuthorizedCardSnapshotReader(
        Path("/unused"), lambda _request: decision, store_factory=factory
    )
    return reader.read(request, now=now), store, factory


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@pytest.mark.parametrize(
    "decision",
    [
        _decision(state="deny"),
        _decision(state="unknown"),
        _decision(state="unavailable"),
        _decision(state="unauthorized"),
        _decision(expires_at=NOW),
        _decision(issued_at=NOW + timedelta(seconds=1)),
        _decision(capauth_decision_id="different"),
        _decision(subject_principal_id="different"),
        _decision(acting_principal_id="different"),
        _decision(node_id="different"),
        _decision(capauth_identity_ref="different"),
        _decision(owner_policy_revision="different"),
        _decision(code="NOT_ALLOW"),
        _decision(scope=AuthorizedCardScopeV1(role="architect")),
        _decision(resource_id="authorized-card-set:sha256:" + "1" * 64),
        _decision(visible_set_sha256="sha256:" + "1" * 64),
        _decision(field_mask=("visible_edges",)),
        _decision(semantic_classes=("project",)),
        _decision(visible_absent=("owner-attested-absent",)),
    ],
)
def test_non_allow_expired_and_mismatched_policy_never_construct_store(
    decision,
) -> None:
    factory = Mock(side_effect=AssertionError("CardStore constructed before allow"))
    reader = AuthorizedCardSnapshotReader(
        Path("/unused"), lambda _request: decision, store_factory=factory
    )
    result = reader.read(REQUEST, now=NOW)
    factory.assert_not_called()
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None
    assert result["watermark"]["value"] is None
    assert result["records"] == result["dependency_edges"] == result["milestones"] == []


def test_policy_failure_invalid_result_and_revocation_are_constant_and_no_read() -> (
    None
):
    factory = Mock(side_effect=AssertionError("CardStore constructed before allow"))
    validators = [
        lambda _request: (_ for _ in ()).throw(RuntimeError("SECRET-POLICY-ERROR")),
        lambda _request: {"state": "allow"},
        lambda _request: _decision(state="deny"),
    ]
    outputs = [
        AuthorizedCardSnapshotReader(
            Path("/unused"), validator, store_factory=factory
        ).read(REQUEST, now=NOW)
        for validator in validators
    ]
    factory.assert_not_called()
    assert outputs[0] == outputs[1] == outputs[2]
    assert "SECRET" not in json.dumps(outputs)


def test_live_policy_revocation_does_not_reuse_an_allowed_snapshot() -> None:
    decisions = iter([_decision(visible=("visible",)), _decision(state="deny")])
    store = _Store([_card("visible")])
    factory = Mock(return_value=store)
    reader = AuthorizedCardSnapshotReader(
        Path("/unused"), lambda _request: next(decisions), store_factory=factory
    )
    allowed = reader.read(_request(("visible",)), now=NOW)
    revoked = reader.read(REQUEST, now=NOW)
    assert allowed["truth_state"] == "current"
    assert revoked["truth_state"] == "unknown"
    assert revoked["records"] == []
    assert factory.call_count == 1
    assert store.fold_calls == ["visible"]


def test_protected_and_missing_store_state_is_byte_identical() -> None:
    public = _card("public", dependencies=["hidden-or-missing"])
    hidden = _card(
        "hidden-or-missing",
        meta={"tenant_id": "SECRET-TENANT", "matter_id": "SECRET-MATTER"},
    )
    decision = _decision(visible=("public",))
    missing, missing_store, _ = _read([public], decision)
    protected, protected_store, _ = _read([public, hidden], decision)
    assert json.dumps(missing, sort_keys=True) == json.dumps(protected, sort_keys=True)
    assert missing_store.fold_calls == protected_store.fold_calls == ["public"]
    assert missing["records"][0]["visible_dependency_count"] == 0
    assert missing["dependency_edges"] == []
    assert "hidden-or-missing" not in json.dumps(missing)
    assert "SECRET" not in json.dumps(protected)


def test_hidden_dependency_volume_cannot_change_authorized_projection() -> None:
    target = _card("target")
    decision = _decision(visible=("source", "target"))
    baseline, _store, _factory = _read(
        [_card("source", dependencies=["target"]), target], decision
    )
    hidden = [f"hidden-{index:03d}" for index in range(128)]
    challenged, _store, _factory = _read(
        [_card("source", dependencies=["target", *hidden]), target], decision
    )
    assert challenged == baseline
    assert baseline["truth_state"] == "current"
    assert baseline["classification_complete"] is True
    assert len(baseline["dependency_edges"]) == 1


def test_unapproved_label_volume_cannot_change_authorized_projection() -> None:
    decision = _decision(visible=("source",))
    baseline, _store, _factory = _read([_card("source", labels=["project"])], decision)
    unapproved = [f"unapproved-{index:03d}" for index in range(128)]
    challenged, _store, _factory = _read(
        [_card("source", labels=["project", *unapproved])], decision
    )
    assert challenged == baseline
    assert baseline["truth_state"] == "current"
    assert baseline["records"][0]["classifications"] == ["project"]


def test_malformed_dependency_container_is_partial_not_empty() -> None:
    source = _card("source", dependencies=["target"])
    source.dependencies = {"target": True}
    result, _store, _factory = _read(
        [source, _card("target")], _decision(visible=("source", "target"))
    )
    assert result["truth_state"] == "partial"
    assert result["classification_complete"] is False
    assert result["dependency_edges"] == []
    assert any(
        error["code"] == "AUTHORIZED_CARD_SNAPSHOT_PARTIAL"
        for error in result["errors"]
    )


def test_malformed_label_container_is_partial_not_empty() -> None:
    source = _card("source", labels=["project"])
    source.labels = {"project": True}
    result, _store, _factory = _read([source], _decision(visible=("source",)))
    assert result["truth_state"] == "partial"
    assert result["classification_complete"] is False
    assert result["records"][0]["classifications"] == []
    assert any(
        error["code"] == "AUTHORIZED_CARD_SNAPSHOT_PARTIAL"
        for error in result["errors"]
    )


def test_empty_and_protected_only_stores_are_byte_identical_without_fold() -> None:
    hidden = _card("hidden", meta={"tenant_id": "SECRET"})
    decision = _decision(visible=())
    empty, empty_store, _ = _read([], decision)
    protected, protected_store, _ = _read([hidden], decision)
    assert empty == protected
    assert empty_store.fold_calls == protected_store.fold_calls == []


def test_orphan_is_emitted_only_for_owner_attested_visible_absence() -> None:
    public = _card("public", dependencies=["attested-absent", "not-attested"])
    decision = _decision(visible=("public",), visible_absent=("attested-absent",))
    result, store, _factory = _read([public], decision)
    assert store.fold_calls == ["public"]
    assert len(result["dependency_edges"]) == 1
    edge = result["dependency_edges"][0]
    assert edge["resolution"] == "orphaned"
    assert edge["to_record_id"] == "attested-absent"
    assert edge["conditions"] == ["owner_attested_absent"]
    assert "not-attested" not in json.dumps(result)


def test_allowed_projection_has_only_visible_edges_and_allowlisted_fields() -> None:
    source = _card(
        "source",
        dependencies=["target", "not-visible"],
        labels=["project", "SECRET-LABEL"],
        meta={"claim_conflicts": [{"secret": "SECRET-META"}]},
    )
    target = _card("target", labels=["human-gate", "milestone"], archived=True)
    result, store, _ = _read([source, target], _decision(visible=("source", "target")))
    assert store.fold_calls == ["source", "target"]
    store.list_card_ids.assert_not_called()
    store.list_cards.assert_not_called()
    store.create.assert_not_called()
    store.append_event.assert_not_called()
    assert result["population_counts"]["authorized_ids"] == 2
    assert result["records"][0]["visible_dependency_count"] in {0, 1}
    edge = result["dependency_edges"][0]
    assert (edge["from_record_id"], edge["to_record_id"]) == ("source", "target")
    assert "human_gated" in edge["conditions"]
    assert "archived_target" in edge["conditions"]
    assert len(result["milestones"]) == 1
    serialized = json.dumps(result)
    for secret in (
        "SECRET-TITLE",
        "SECRET-DESCRIPTION",
        "SECRET-ORIGINATOR",
        "SECRET-CRITERIA",
        "SECRET-CAPABILITY",
        "SECRET-LABEL",
        "SECRET-META",
        "not-visible",
    ):
        assert secret not in serialized
    assert not set(_keys(result)) & {
        "acceptance_criteria",
        "capability",
        "description",
        "links",
        "matter_id",
        "meta",
        "originator",
        "raw_capability_token",
        "tenant_id",
        "title",
    }


def test_reader_source_has_no_enumeration_private_event_or_mutation_call() -> None:
    source = inspect.getsource(AuthorizedCardSnapshotReader)
    for forbidden in (
        "list_card_ids(",
        "list_cards(",
        "_read_events(",
        "append_event(",
        "create(",
        "archive(",
    ):
        assert forbidden not in source


def test_real_card_store_snapshot_read_changes_no_source_bytes_or_mtimes(
    tmp_path: Path,
) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="visible", title="Setup", created_by="fixture"))
    before = {
        path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    decision = _decision(visible=("visible",))
    reader = AuthorizedCardSnapshotReader(
        tmp_path,
        lambda _request: decision,
        store_factory=lambda _home: store,
    )
    result = reader.read(_request(("visible",)), now=NOW)
    after = {
        path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result["truth_state"] == "current"
    assert before == after


def test_field_mask_is_enforced_before_derived_evidence() -> None:
    source = _card(
        "source",
        dependencies=["target"],
        labels=["project"],
        meta={"claim_conflicts": [{"private": "SECRET"}]},
    )
    target = _card("target", labels=["human-gate", "milestone"])
    decision = _decision(
        visible=("source", "target"),
        field_mask=(),
        semantic_classes=(),
        resource_id=authorized_card_resource_id(
            ("source", "target"), (), (), scope=SCOPE
        ),
    )
    result, _store, _factory = _read([source, target], decision)
    assert result["dependency_edges"] == []
    assert result["milestones"] == []
    assert all(record["owner"] is None for record in result["records"])
    assert all(record["classifications"] == [] for record in result["records"])
    assert "SECRET" not in json.dumps(result)


def test_cycle_claim_conflict_stale_future_and_milestone_paths_are_distinct() -> None:
    cards = [
        _card("milestone", dependencies=["middle"], labels=["milestone"]),
        _card("middle", dependencies=["gate"]),
        _card(
            "gate",
            dependencies=["middle"],
            labels=["human-gate"],
            updated_at="2026-01-01T00:00:00Z",
            meta={"claim_conflicts": [{"safe": "derived-only"}]},
        ),
        _card("future", updated_at="2099-01-01T00:00:00Z"),
        _card("future-source", dependencies=["future"]),
    ]
    visible = tuple(sorted(card.id for card in cards))
    result, _store, _factory = _read(cards, _decision(visible=visible))
    edge = next(
        finding
        for finding in result["dependency_edges"]
        if finding["from_record_id"] == "milestone"
    )
    assert {
        "stale",
        "conflicted",
        "record_claim_conflict",
        "human_gated",
        "milestone_path",
    } <= set(edge["conditions"])
    assert any(
        "dependency_cycle" in finding["conditions"]
        for finding in result["dependency_edges"]
    )
    future = next(
        finding
        for finding in result["dependency_edges"]
        if finding["from_record_id"] == "future-source"
    )
    assert "freshness_unknown" in future["conditions"]
    summary = result["milestones"][0]["dependency_path_summary"]
    assert summary["conditions"]["human_gated"] >= 1
    assert summary["conditions"]["dependency_cycle"] >= 1


def test_concurrent_fold_gap_is_partial_and_never_called_an_orphan() -> None:
    decision = _decision(visible=("gone", "source"))
    result, store, _ = _read([_card("source", dependencies=["gone"])], decision)
    assert store.fold_calls == ["gone", "source"]
    assert result["truth_state"] == "partial"
    assert result["classification_complete"] is False
    assert result["dependency_edges"] == []
    assert all("orphan" not in json.dumps(error).lower() for error in result["errors"])


def test_malicious_long_fields_and_milestones_stay_under_utf8_byte_cap() -> None:
    cards = [
        _card(
            f"card-{index:04d}",
            labels=["milestone"],
            owner="🧨" * 10_000,
            priority="🧨" * 10_000,
            created_at="🧨" * 10_000,
            updated_at="🧨" * 10_000,
            meta={"notes": "🧨" * 100_000},
        )
        for index in range(200)
    ]
    visible = tuple(card.id for card in cards)
    result, _store, _factory = _read(cards, _decision(visible=visible))
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= MAX_PROJECT_ITEM_BYTES
    assert result["truth_state"] == "partial"
    assert "🧨" not in encoded.decode("utf-8")


def test_final_oversize_projection_is_replaced_by_constant_no_value() -> None:
    cards = [_card(f"card-{index:04d}", labels=["milestone"]) for index in range(200)]
    visible = tuple(card.id for card in cards)
    with patch.object(
        AuthorizedCardSnapshotReader,
        "_milestone",
        return_value={"private": "SECRET-OVERSIZE" * 10_000},
    ):
        result, _store, _factory = _read(cards, _decision(visible=visible))
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= MAX_PROJECT_ITEM_BYTES
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None
    assert result["records"] == result["dependency_edges"] == result["milestones"] == []
    assert "SECRET-OVERSIZE" not in encoded.decode("utf-8")


def test_watermark_is_deterministic_and_sensitive_only_to_visible_facts() -> None:
    decision = _decision(visible=("a", "b"))
    cards = [_card("a", dependencies=["b"]), _card("b")]
    first, _store, _factory = _read(cards, decision)
    second, _store, _factory = _read(list(reversed(cards)), decision)
    assert first["watermark"] == second["watermark"]
    changed, _store, _factory = _read([_card("a"), _card("b")], decision)
    assert changed["watermark"] != first["watermark"]


def test_scope_and_decision_contract_reject_protected_or_oversized_values() -> None:
    with pytest.raises(ValidationError):
        AuthorizedCardScopeV1(role="project-manager", tenant_id="secret")
    with pytest.raises(ValidationError):
        AuthorizedCardSnapshotRequestV1(
            identity=IDENTITY,
            scope=SCOPE,
            resource_id="x" * 129,
            capauth_decision_id="capauth-decision-01",
            owner_policy_revision="owner-policy-r1",
        )
    with pytest.raises(ValidationError):
        _decision(visible=tuple(f"card-{index}" for index in range(2_001)))
    with pytest.raises(ValidationError):
        _decision(state="deny", visible_absent=("attested-absent",))
    with pytest.raises(ValidationError):
        _decision(visible=("same",), visible_absent=("same",))
