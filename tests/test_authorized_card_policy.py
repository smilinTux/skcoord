from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from capauth import (
    CapabilityAuthorizer,
    CapabilityIssuer,
    ClientKind,
    ControlPlaneBinding,
    ControlPlaneDecisionAuthorizer,
    ControlPlaneInvocationV1,
    InMemoryAuditSink,
    InMemoryPrincipalPolicyBackend,
    InMemoryReplayBackend,
    InMemoryRevocationBackend,
    IssuerGrant,
    Principal,
    RequestBoundary,
    StaticTrustedIssuerBackend,
    export_control_plane_bearer,
    parse_control_plane_bearer,
    parse_presented_token,
)
from pydantic import ValidationError

from skcoord.authorized_card_policy import (
    AuthorizedCardPolicyEntryV1,
    AuthorizedCardPolicyProvider,
    StaticAuthorizedCardPolicyBackend,
)
from skcoord.authorized_card_snapshot import AuthorizedCardScopeV1
from skcoord.card import Card, Column, Kind

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
ORIGIN = "https://dashboard.example.test"
ISSUER = "A" * 40
MASK = ("human_gate", "orphan_evidence", "visible_edges")
CLASSES = ("human-gate", "project")
SCOPE = AuthorizedCardScopeV1(role="project-manager")


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class Signer:
    def __init__(self) -> None:
        self.values = {}
        self.counter = 0

    @property
    def issuer_fingerprint(self) -> str:
        return ISSUER

    def sign(self, payload_bytes: bytes) -> str:
        self.counter += 1
        signature = f"qualification-signature-{self.counter}"
        self.values[signature] = payload_bytes
        return signature

    def verify(self, token) -> bool:
        return self.values.get(token.signature or "") == token.payload.model_dump_json().encode()


class Store:
    def __init__(self, cards=()) -> None:
        self.cards = {card.id: card for card in cards}
        self.fold_calls = []
        self.list_card_ids = Mock(side_effect=AssertionError("identifier enumeration called"))
        self.list_cards = Mock(side_effect=AssertionError("record enumeration called"))
        self.create = Mock(side_effect=AssertionError("mutation called"))
        self.append_event = Mock(side_effect=AssertionError("mutation called"))

    def fold(self, card_id):
        self.fold_calls.append(card_id)
        return self.cards.get(card_id)


def card(card_id, *, dependencies=(), labels=()):
    return Card(
        id=card_id,
        kind=Kind.TASK,
        title="content is not projected",
        description="SECRET-DESCRIPTION",
        status=Column.BACKLOG,
        swimlane="feature",
        priority="medium",
        originator="SECRET-ORIGINATOR",
        owner=None,
        labels=list(labels),
        acceptance_criteria=["SECRET-CRITERIA"],
        dependencies=list(dependencies),
        links={"token": "SECRET-TOKEN"},
        meta={},
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )


def entry(**changes) -> AuthorizedCardPolicyEntryV1:
    values = {
        "subject": "human@example.test",
        "acting_principal_id": "human-1",
        "node_id": "chiap04",
        "scope": SCOPE,
        "valid_from": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=5),
        "visible_card_ids": ("source",),
        "visible_absent_ids": (),
        "field_mask": MASK,
        "semantic_classes": CLASSES,
    }
    values.update(changes)
    return AuthorizedCardPolicyEntryV1.issue(**values)


class Rig:
    def __init__(self, policy_entry=None, *, store=None, backend=None, principal=None) -> None:
        self.clock = Clock()
        self.entry = policy_entry or entry()
        self.backend = backend or StaticAuthorizedCardPolicyBackend((self.entry,))
        self.store = store or Store((card("source", labels=("project",)),))
        self.principal = principal or Principal(
            principal_id="human-1", subject="human@example.test", kind="human"
        )
        self.binding = ControlPlaneBinding(
            principal=self.principal,
            node_id="chiap04",
            purpose="project-management-reporting",
            capability="skdashboard.read",
            target="/api/v1/overview",
            resource_type="skcoord.card_store.project_snapshot",
            resource_id=self.entry.resource_id,
            owner_policy_revision=self.entry.owner_policy_revision,
            expires_at=NOW + timedelta(minutes=1),
        )
        signer = Signer()
        self.trusted_issuers = StaticTrustedIssuerBackend(
            (
                IssuerGrant(
                    fingerprint=ISSUER,
                    capabilities=frozenset({"skdashboard.read"}),
                    audiences=frozenset({"skdashboard"}),
                    principal_kinds=frozenset({"human"}),
                ),
            )
        )
        self.principals = InMemoryPrincipalPolicyBackend((self.principal,))
        self.revocations = InMemoryRevocationBackend()
        self.provider = AuthorizedCardPolicyProvider(
            self.backend,
            clock=self.clock,
            store_factory=Mock(return_value=self.store),
        )
        capability = CapabilityAuthorizer(
            trusted_issuers=self.trusted_issuers,
            principals=self.principals,
            revocations=self.revocations,
            replay=InMemoryReplayBackend(clock=self.clock),
            audit=InMemoryAuditSink(),
            signature_verifier=signer,
            clock=self.clock,
        )
        self.issuer = CapabilityIssuer(signer, clock=self.clock)
        self.authorizer = ControlPlaneDecisionAuthorizer(
            capability_authorizer=capability,
            owner_policy=self.provider,
            allowed_origins=frozenset({ORIGIN}),
            clock=self.clock,
        )

    def bearer(self):
        presented = self.issuer.issue_root(
            principal=self.principal,
            scope=self.binding.capability_scope(),
            ttl_seconds=60,
        )
        return export_control_plane_bearer(presented)

    def invocation(self):
        return ControlPlaneInvocationV1(
            node_id=self.binding.node_id,
            purpose=self.binding.purpose,
            capability=self.binding.capability,
            target=self.binding.target,
            resource_type=self.binding.resource_type,
            resource_id=self.binding.resource_id,
            correlation_id="request-1",
            boundary=RequestBoundary(client_kind=ClientKind.BROWSER, origin=ORIGIN),
        )

    def authorize(self, bearer=None):
        return self.authorizer.authorize(bearer or self.bearer(), self.invocation())

    def authorize_current(self, bearer=None, *, decision_id: str | None = None):
        bearer = bearer or self.bearer()
        if decision_id is None:
            return self.authorizer.authorize_with_currentness(bearer, self.invocation())
        with patch("capauth.delegated.uuid4", return_value=UUID(decision_id)):
            return self.authorizer.authorize_with_currentness(bearer, self.invocation())


def current_authority(rig: Rig, *, decision_id: str | None = None):
    result, verifier = rig.authorize_current(decision_id=decision_id)
    assert result.allow and result.context is not None
    assert verifier is not None
    return result.context, verifier


def test_real_capauth_context_reads_only_policy_selected_ids() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)

    projection = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert projection["truth_state"] == "current"
    assert projection["population_counts"]["authorized_ids"] == 1
    assert projection["records"][0]["record_id"] == "source"
    assert rig.store.fold_calls == ["source"]
    rig.store.list_card_ids.assert_not_called()
    rig.store.list_cards.assert_not_called()
    rig.store.create.assert_not_called()
    rig.store.append_event.assert_not_called()


def test_hidden_record_and_missing_record_are_byte_identical() -> None:
    public = card("source", dependencies=("hidden",), labels=("project",))
    missing = Rig(store=Store((public,)))
    decision_id = "00000000-0000-0000-0000-000000000001"
    missing_context, missing_verifier = current_authority(missing, decision_id=decision_id)
    protected = AuthorizedCardPolicyProvider(
        missing.backend,
        clock=missing.clock,
        store_factory=Mock(return_value=Store((public, card("hidden", labels=("human-gate",))))),
    )

    one = missing.provider.read(
        missing_context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=missing_verifier,
        now=NOW,
    )
    protected_context, protected_verifier = current_authority(missing, decision_id=decision_id)
    two = protected.read(
        protected_context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=protected_verifier,
        now=NOW,
    )

    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert "hidden" not in json.dumps(one)


def test_empty_and_protected_only_store_are_byte_identical() -> None:
    policy = entry(visible_card_ids=())
    empty = Rig(policy, store=Store(()))
    decision_id = "00000000-0000-0000-0000-000000000002"
    context, verifier = current_authority(empty, decision_id=decision_id)
    protected = AuthorizedCardPolicyProvider(
        empty.backend,
        clock=empty.clock,
        store_factory=Mock(return_value=Store((card("protected"),))),
    )

    one = empty.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )
    protected_context, protected_verifier = current_authority(empty, decision_id=decision_id)
    two = protected.read(
        protected_context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=protected_verifier,
        now=NOW,
    )

    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert one["population_counts"]["authorized_ids"] == 0


def test_only_policy_attested_absence_can_produce_orphan_evidence() -> None:
    attested = entry(visible_absent_ids=("absent",))
    rig = Rig(
        attested,
        store=Store((card("source", dependencies=("absent", "unselected")),)),
    )
    context, verifier = current_authority(rig)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert result["population_counts"]["attested_orphan_edges"] == 1
    assert result["dependency_edges"][0]["to_record_id"] == "absent"
    assert "unselected" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"visible_set_sha256": "sha256:" + "0" * 64},
        {"resource_id": "authorized-card-set:sha256:" + "0" * 64},
        {"owner_policy_revision": "0" * 64},
        {"field_mask": ("not-approved",)},
        {"semantic_classes": ("not-approved",)},
    ],
)
def test_policy_entry_rejects_hash_revision_mask_and_class_tampering(changes) -> None:
    valid = entry().model_dump()
    valid.update(changes)
    with pytest.raises(ValidationError):
        AuthorizedCardPolicyEntryV1(**valid)


def test_policy_entry_rejects_oversize_population() -> None:
    with pytest.raises(ValidationError):
        entry(visible_card_ids=tuple(f"card-{index:04d}" for index in range(2001)))


@pytest.mark.parametrize("card_id", ("../protected", "nested/card", "bad\nvalue", "x" * 129))
def test_policy_entry_rejects_malformed_identifiers(card_id) -> None:
    with pytest.raises(ValidationError):
        entry(visible_card_ids=(card_id,))


@pytest.mark.parametrize(
    "changes",
    (
        {"visible_card_ids": ["source"]},
        {"visible_card_ids": "source"},
        {"visible_card_ids": ("z", "a")},
        {"field_mask": ("visible_edges", "human_gate")},
    ),
)
def test_policy_factory_rejects_mutable_or_noncanonical_collections(changes) -> None:
    with pytest.raises((TypeError, ValidationError)):
        entry(**changes)


def test_resource_binding_changes_with_role_scope() -> None:
    project = entry()
    architect = entry(scope=AuthorizedCardScopeV1(role="architect"))
    assert project.resource_id != architect.resource_id
    assert project.owner_policy_revision != architect.owner_policy_revision


def test_expired_or_changed_policy_returns_constant_before_store() -> None:
    initial = entry()
    rig = Rig(initial)
    context, missing_verifier = current_authority(rig)
    unavailable = AuthorizedCardPolicyProvider(
        StaticAuthorizedCardPolicyBackend(()),
        clock=rig.clock,
        store_factory=Mock(side_effect=AssertionError("store constructed")),
    )
    stale = context.model_copy(
        update={
            "expires_at": NOW,
            "binding": context.binding.model_copy(update={"expires_at": NOW}),
        }
    )

    missing = unavailable.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=missing_verifier,
        now=NOW,
    )
    _, expired_verifier = current_authority(rig)
    expired = rig.provider.read(
        stale,
        SCOPE,
        Path("/unused"),
        currentness_verifier=expired_verifier,
        now=NOW,
    )

    assert json.dumps(missing, sort_keys=True) == json.dumps(expired, sort_keys=True)
    assert missing["truth_state"] == "unknown"
    assert missing["population_counts"] is None


def test_scope_and_attempt_tampering_fail_before_store() -> None:
    rig = Rig()
    context, scope_verifier = current_authority(rig)
    factory = Mock(side_effect=AssertionError("store constructed"))
    provider = AuthorizedCardPolicyProvider(
        rig.backend,
        clock=rig.clock,
        store_factory=factory,
    )
    attempt_two = context.model_copy(
        update={
            "capauth_decision": context.capauth_decision.model_copy(update={"attempt_sequence": 2})
        }
    )

    wrong_scope = provider.read(
        context,
        AuthorizedCardScopeV1(role="architect"),
        Path("/unused"),
        currentness_verifier=scope_verifier,
        now=NOW,
    )
    _, attempt_verifier = current_authority(rig)
    wrong_attempt = provider.read(
        attempt_two,
        SCOPE,
        Path("/unused"),
        currentness_verifier=attempt_verifier,
        now=NOW,
    )

    factory.assert_not_called()
    assert wrong_scope["truth_state"] == wrong_attempt["truth_state"] == "unknown"


def test_joined_context_tampering_is_revalidated_before_store() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)
    factory = Mock(side_effect=AssertionError("store constructed"))
    provider = AuthorizedCardPolicyProvider(
        rig.backend,
        clock=rig.clock,
        store_factory=factory,
    )
    forged = context.model_copy(
        update={
            "joined_decision": context.joined_decision.model_copy(
                update={"capauth_decision_id": "forged-decision"}
            )
        }
    )

    result = provider.read(
        forged,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    factory.assert_not_called()
    assert result["truth_state"] == "unknown"


class SequenceBackend:
    def __init__(self, values):
        self.values = list(values)
        self.lock = Lock()

    def snapshot(self, _selection):
        with self.lock:
            return self.values.pop(0) if self.values else None


class RevisionFlipBackend:
    def __init__(self, initial, changed):
        self.initial = initial
        self.changed = changed

    def snapshot(self, _selection):
        return self.initial

    def read_if_current(self, _selection, expected_revision, _operation):
        self.initial = self.changed
        assert self.initial.owner_policy_revision != expected_revision
        return None


def test_policy_revision_change_between_owner_reads_denies() -> None:
    initial = entry()
    changed = entry(expires_at=NOW + timedelta(minutes=4))
    rig = Rig(initial, backend=SequenceBackend((initial, changed)))

    result = rig.authorize()

    assert result.allow is False
    assert result.context is None


def test_revoked_followup_has_no_context_and_cannot_read() -> None:
    rig = Rig()
    first = rig.authorize()
    assert first.context is not None
    bearer = rig.bearer()
    raw = parse_control_plane_bearer(bearer).credentials_for_verification()[-1]
    rig.revocations.revoke(parse_presented_token(raw).credential_digest)
    second = rig.authorize(bearer)

    assert second.allow is False
    assert second.context is None


def test_revoked_existing_context_cannot_construct_or_read_store() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)
    digest = context.capauth_decision.credential_digest
    assert digest is not None
    rig.revocations.revoke(digest)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None
    assert rig.store.fold_calls == []


def test_revocation_during_owner_read_suppresses_private_result() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)
    digest = context.capauth_decision.credential_digest
    assert digest is not None

    class RevokingStore(Store):
        def fold(self, card_id):
            value = super().fold(card_id)
            rig.revocations.revoke(digest)
            return value

    store = RevokingStore((card("source", labels=("project",)),))
    rig.provider._store_factory = Mock(return_value=store)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert store.fold_calls == ["source"]
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None
    assert result["records"] == []


def test_missing_fake_and_reused_verifiers_fail_closed() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)

    missing = rig.provider.read(context, SCOPE, Path("/unused"), now=NOW)
    fake = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=object(),  # type: ignore[arg-type]
        now=NOW,
    )
    first = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )
    rig.store.fold_calls.clear()
    reused = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert missing["truth_state"] == fake["truth_state"] == "unknown"
    assert first["truth_state"] == "current"
    assert reused["truth_state"] == "unknown"
    assert rig.store.fold_calls == []


def test_malformed_scope_closes_verifier_and_cannot_be_retried() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)

    malformed = rig.provider.read(
        context,
        object(),  # type: ignore[arg-type]
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )
    retry = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert malformed["truth_state"] == retry["truth_state"] == "unknown"
    assert malformed["scope"] == AuthorizedCardScopeV1(role="operator").model_dump()
    assert rig.store.fold_calls == []


def test_provider_rejects_legacy_permissive_currentness_callback() -> None:
    with pytest.raises(TypeError):
        AuthorizedCardPolicyProvider(  # type: ignore[call-arg]
            StaticAuthorizedCardPolicyBackend((entry(),)),
            context_is_current=lambda *_args: True,
        )


def test_policy_revision_change_before_atomic_read_never_constructs_store() -> None:
    initial = entry()
    changed = entry(expires_at=NOW + timedelta(minutes=4))
    rig = Rig(initial)
    context, verifier = current_authority(rig)
    factory = Mock(side_effect=AssertionError("store constructed"))
    provider = AuthorizedCardPolicyProvider(
        RevisionFlipBackend(initial, changed),
        clock=rig.clock,
        store_factory=factory,
    )

    result = provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    factory.assert_not_called()
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None


def test_context_expiring_during_read_suppresses_projection() -> None:
    rig = Rig()
    context, verifier = current_authority(rig)

    def advance_clock(*_args):
        rig.clock.value = NOW + timedelta(minutes=2)
        return rig.store

    rig.provider._store_factory = Mock(side_effect=advance_clock)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
    )

    assert rig.store.fold_calls == ["source"]
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None


def test_owner_policy_expiring_during_read_suppresses_projection() -> None:
    policy = entry(expires_at=NOW + timedelta(seconds=30))
    rig = Rig(policy)
    context, verifier = current_authority(rig)

    def advance_clock(*_args):
        rig.clock.value = NOW + timedelta(seconds=45)
        return rig.store

    rig.provider._store_factory = Mock(side_effect=advance_clock)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
    )

    assert rig.store.fold_calls == ["source"]
    assert result["truth_state"] == "unknown"
    assert result["population_counts"] is None


def test_concurrent_principals_remain_isolated() -> None:
    second = entry(
        subject="architect@example.test",
        acting_principal_id="architect-1",
        scope=AuthorizedCardScopeV1(role="architect"),
        visible_card_ids=("architect-card",),
    )
    backend = StaticAuthorizedCardPolicyBackend((entry(), second))
    outputs = {}
    lock = Lock()

    def read_policy(name, policy, principal):
        store = Store((card(policy.visible_card_ids[0]),))
        rig = Rig(policy, backend=backend, principal=principal, store=store)
        context, verifier = current_authority(rig)
        result = rig.provider.read(
            context,
            policy.scope,
            Path("/unused"),
            currentness_verifier=verifier,
            now=NOW,
        )
        with lock:
            outputs[name] = result["records"][0]["record_id"]

    threads = [
        Thread(
            target=read_policy,
            args=(
                "project",
                entry(),
                Principal(principal_id="human-1", subject="human@example.test", kind="human"),
            ),
        ),
        Thread(
            target=read_policy,
            args=(
                "architect",
                second,
                Principal(
                    principal_id="architect-1",
                    subject="architect@example.test",
                    kind="human",
                ),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outputs == {"project": "source", "architect": "architect-card"}


def test_projection_respects_owner_wire_ceiling() -> None:
    visible = tuple(f"card-{index:04d}" for index in range(300))
    policy = entry(visible_card_ids=visible)
    store = Store(tuple(card(card_id) for card_id in visible))
    rig = Rig(policy, store=store)
    context, verifier = current_authority(rig)

    result = rig.provider.read(
        context,
        SCOPE,
        Path("/unused"),
        currentness_verifier=verifier,
        now=NOW,
    )

    assert len(json.dumps(result, sort_keys=True).encode()) <= 384 * 1024
    assert result["truncated"] is True
