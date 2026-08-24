"""Owner-policy decisions for bounded authorized CardStore snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from capauth import (
    AuthorizationDecision,
    ControlPlaneBinding,
    ControlPlaneCurrentnessVerifier,
    DecisionCode,
    DecisionReason,
    DecisionState,
    OwnerPolicyDecision,
    SanitizedControlPlaneDecisionV1,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .authorized_card_snapshot import (
    FIELD_MASK_VALUES,
    MAX_VISIBLE_RECORDS,
    SEMANTIC_LABELS,
    AuthorizedCardIdentityV1,
    AuthorizedCardScopeV1,
    AuthorizedCardSetDecisionV1,
    AuthorizedCardSnapshotReader,
    AuthorizedCardSnapshotRequestV1,
    authorized_card_resource_id,
    unavailable_authorized_card_snapshot,
    visible_set_sha256,
)
from .card_store import CardStore

UTC = timezone.utc
MAX_POLICY_ENTRY_BYTES = 384 * 1024


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 128
        and value.isascii()
        and value not in {".", ".."}
        and ".." not in value
        and "/" not in value
        and "\\" not in value
        and all(32 <= ord(char) < 127 for char in value)
    )


def _entry_facts(values: dict) -> dict:
    return {
        "subject": values["subject"],
        "acting_principal_id": values["acting_principal_id"],
        "node_id": values["node_id"],
        "scope": values["scope"].model_dump(mode="json"),
        "valid_from": values["valid_from"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "expires_at": values["expires_at"].astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "visible_card_ids": list(values["visible_card_ids"]),
        "visible_absent_ids": list(values["visible_absent_ids"]),
        "field_mask": list(values["field_mask"]),
        "semantic_classes": list(values["semantic_classes"]),
    }


class AuthorizedCardPolicyEntryV1(_Contract):
    """One immutable owner-policy selection for an exact control-plane read."""

    subject: str = Field(min_length=1, max_length=128)
    acting_principal_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    scope: AuthorizedCardScopeV1
    valid_from: datetime
    expires_at: datetime
    visible_card_ids: tuple[str, ...] = ()
    visible_absent_ids: tuple[str, ...] = ()
    field_mask: tuple[str, ...] = ()
    semantic_classes: tuple[str, ...] = ()
    visible_set_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resource_id: str = Field(pattern=r"^authorized-card-set:sha256:[0-9a-f]{64}$")
    owner_policy_revision: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("visible_card_ids", "visible_absent_ids")
    @classmethod
    def canonical_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(not _identifier(value) for value in values):
            raise ValueError("policy identifiers must be sorted and unique")
        return values

    @field_validator("subject", "acting_principal_id", "node_id")
    @classmethod
    def safe_identity(cls, value: str) -> str:
        if not _identifier(value):
            raise ValueError("policy identity is invalid")
        return value

    @field_validator("field_mask")
    @classmethod
    def canonical_mask(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or not set(values) <= set(FIELD_MASK_VALUES):
            raise ValueError("policy field mask is not canonical")
        return values

    @field_validator("semantic_classes")
    @classmethod
    def canonical_classes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or not set(values) <= SEMANTIC_LABELS:
            raise ValueError("policy semantic classes are not canonical")
        return values

    @model_validator(mode="after")
    def exact_bindings(self) -> "AuthorizedCardPolicyEntryV1":
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() != timedelta(0):
            raise ValueError("policy valid_from must use UTC offset zero")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() != timedelta(0):
            raise ValueError("policy expires_at must use UTC offset zero")
        if self.valid_from >= self.expires_at:
            raise ValueError("policy validity interval is empty")
        if len(self.visible_card_ids) + len(self.visible_absent_ids) > MAX_VISIBLE_RECORDS:
            raise ValueError("policy population exceeds the safe cap")
        if set(self.visible_card_ids) & set(self.visible_absent_ids):
            raise ValueError("visible and absent policy identifiers overlap")
        facts = _entry_facts(self.__dict__)
        if len(_canonical_bytes(facts)) > MAX_POLICY_ENTRY_BYTES:
            raise ValueError("policy entry exceeds the safe serialized cap")
        expected_resource = authorized_card_resource_id(
            self.visible_card_ids,
            self.field_mask,
            self.semantic_classes,
            self.visible_absent_ids,
            scope=self.scope,
        )
        if self.visible_set_sha256 != visible_set_sha256(self.visible_card_ids):
            raise ValueError("visible identifier hash does not match policy facts")
        if self.resource_id != expected_resource:
            raise ValueError("resource id does not match policy facts")
        if self.owner_policy_revision != _sha256(facts):
            raise ValueError("owner policy revision does not match policy facts")
        return self

    @classmethod
    def issue(
        cls,
        *,
        subject: str,
        acting_principal_id: str,
        node_id: str,
        scope: AuthorizedCardScopeV1,
        valid_from: datetime,
        expires_at: datetime,
        visible_card_ids: tuple[str, ...] = (),
        visible_absent_ids: tuple[str, ...] = (),
        field_mask: tuple[str, ...] = (),
        semantic_classes: tuple[str, ...] = (),
    ) -> "AuthorizedCardPolicyEntryV1":
        collections = (
            visible_card_ids,
            visible_absent_ids,
            field_mask,
            semantic_classes,
        )
        if any(not isinstance(value, tuple) for value in collections):
            raise TypeError("policy collections must be immutable tuples")
        values = {
            "subject": subject,
            "acting_principal_id": acting_principal_id,
            "node_id": node_id,
            "scope": scope,
            "valid_from": valid_from,
            "expires_at": expires_at,
            "visible_card_ids": visible_card_ids,
            "visible_absent_ids": visible_absent_ids,
            "field_mask": field_mask,
            "semantic_classes": semantic_classes,
        }
        facts = _entry_facts(values)
        return cls(
            **values,
            visible_set_sha256=visible_set_sha256(values["visible_card_ids"]),
            resource_id=authorized_card_resource_id(
                values["visible_card_ids"],
                values["field_mask"],
                values["semantic_classes"],
                values["visible_absent_ids"],
                scope=scope,
            ),
            owner_policy_revision=_sha256(facts),
        )


class AuthorizedCardPolicySelectionV1(_Contract):
    """Exact O(1) owner-policy lookup key derived from a signed binding."""

    subject: str = Field(min_length=1, max_length=128)
    acting_principal_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(pattern=r"^authorized-card-set:sha256:[0-9a-f]{64}$")


class AuthorizedCardPolicyBackend(Protocol):
    """Trusted owner-policy snapshot source supplied by application composition."""

    def snapshot(
        self, selection: AuthorizedCardPolicySelectionV1
    ) -> AuthorizedCardPolicyEntryV1 | None: ...

    def read_if_current(
        self,
        selection: AuthorizedCardPolicySelectionV1,
        expected_revision: str,
        operation: Callable[[AuthorizedCardPolicyEntryV1], dict],
    ) -> dict | None:
        """Run one read while the selected owner-policy revision is current."""
        ...


class StaticAuthorizedCardPolicyBackend:
    """Immutable policy backend for trusted static composition and qualification."""

    __slots__ = ("_entries",)

    def __init__(self, entries: Iterable[AuthorizedCardPolicyEntryV1]) -> None:
        indexed = {}
        for entry in entries:
            if not isinstance(entry, AuthorizedCardPolicyEntryV1):
                raise TypeError("policy backend requires strict policy entries")
            key = self._key(entry)
            if key in indexed:
                raise ValueError("duplicate authorized card policy selection")
            indexed[key] = entry
        self._entries = MappingProxyType(indexed)

    @staticmethod
    def _key(entry: AuthorizedCardPolicyEntryV1) -> tuple[str, str, str, str]:
        return (entry.subject, entry.acting_principal_id, entry.node_id, entry.resource_id)

    def snapshot(
        self, selection: AuthorizedCardPolicySelectionV1
    ) -> AuthorizedCardPolicyEntryV1 | None:
        if not isinstance(selection, AuthorizedCardPolicySelectionV1):
            return None
        return self._entries.get(
            (
                selection.subject,
                selection.acting_principal_id,
                selection.node_id,
                selection.resource_id,
            )
        )

    def read_if_current(
        self,
        selection: AuthorizedCardPolicySelectionV1,
        expected_revision: str,
        operation: Callable[[AuthorizedCardPolicyEntryV1], dict],
    ) -> dict | None:
        entry = self.snapshot(selection)
        if entry is None or entry.owner_policy_revision != expected_revision:
            return None
        return operation(entry)


class AuthorizedCardPolicyProvider:
    """Join owner policy to CapAuth and issue request-local snapshot decisions."""

    def __init__(
        self,
        backend: AuthorizedCardPolicyBackend,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        store_factory=CardStore,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._store_factory = store_factory

    def decide(
        self, binding: ControlPlaneBinding, capauth_decision: AuthorizationDecision
    ) -> OwnerPolicyDecision | None:
        try:
            now = self._current()
            entry = self._resolve(binding, capauth_decision, now)
        except Exception:
            return None
        if entry is None:
            return None
        return OwnerPolicyDecision(
            state=DecisionState.ALLOW,
            revision=entry.owner_policy_revision,
            resource_type=binding.resource_type,
            resource_id=entry.resource_id,
            reason_code="authorized_card_set_allow",
        )

    def read(
        self,
        context: SanitizedControlPlaneDecisionV1,
        requested_scope: AuthorizedCardScopeV1,
        home: Path,
        *,
        currentness_verifier: ControlPlaneCurrentnessVerifier | None = None,
        now: datetime | None = None,
    ) -> dict:
        try:
            fallback_scope = AuthorizedCardScopeV1(role="operator")
            denied = unavailable_authorized_card_snapshot(fallback_scope)
            if type(requested_scope) is not AuthorizedCardScopeV1:
                return denied
            validated_scope = AuthorizedCardScopeV1.model_validate(
                requested_scope.model_dump(mode="python")
            )
            if validated_scope != requested_scope:
                return denied
            denied = unavailable_authorized_card_snapshot(validated_scope)
            if type(currentness_verifier) is not ControlPlaneCurrentnessVerifier:
                return denied
            current = self._current(now)
            if not isinstance(context, SanitizedControlPlaneDecisionV1):
                return denied
            validated = SanitizedControlPlaneDecisionV1(
                binding=context.binding,
                boundary=context.boundary,
                capauth_decision=context.capauth_decision,
                joined_decision=context.joined_decision,
                authenticated_identity_ref=context.authenticated_identity_ref,
                issued_at=context.issued_at,
                expires_at=context.expires_at,
            )
            if validated != context:
                return denied
            if currentness_verifier.check_before_owner_read(context) is not DecisionState.ALLOW:
                return denied
            binding = context.binding
            capauth = context.capauth_decision
            joined = context.joined_decision
            entry = self._resolve(binding, capauth, current)
            if entry is None or entry.scope != validated_scope:
                return denied
            if (
                not joined.allow
                or joined.state is not DecisionState.ALLOW
                or joined.code is not DecisionCode.ALLOW
                or capauth.attempt_sequence != 1
                or context.issued_at > current
                or current >= context.expires_at
                or capauth.decision_id != joined.capauth_decision_id
                or binding.owner_policy_revision != entry.owner_policy_revision
                or binding.resource_id != entry.resource_id
            ):
                return denied
            acting = binding.agent_id or binding.principal.principal_id
            request = AuthorizedCardSnapshotRequestV1(
                identity=AuthorizedCardIdentityV1(
                    subject_principal_id=binding.principal.subject,
                    acting_principal_id=acting,
                    node_id=binding.node_id,
                    capauth_identity_ref=context.authenticated_identity_ref,
                ),
                scope=validated_scope,
                resource_id=entry.resource_id,
                capauth_decision_id=capauth.decision_id,
                owner_policy_revision=entry.owner_policy_revision,
            )
            decision = AuthorizedCardSetDecisionV1(
                capauth_decision_id=capauth.decision_id,
                owner_policy_revision=entry.owner_policy_revision,
                state="allow",
                code="ALLOW",
                subject_principal_id=binding.principal.subject,
                acting_principal_id=acting,
                node_id=binding.node_id,
                capauth_identity_ref=context.authenticated_identity_ref,
                resource_id=entry.resource_id,
                scope=entry.scope,
                issued_at=max(context.issued_at, entry.valid_from),
                expires_at=min(context.expires_at, entry.expires_at),
                visible_card_ids=entry.visible_card_ids,
                visible_absent_ids=entry.visible_absent_ids,
                visible_set_sha256=entry.visible_set_sha256,
                field_mask=entry.field_mask,
                semantic_classes=entry.semantic_classes,
            )
            selection = self._selection(binding)

            def read_current(current_entry: AuthorizedCardPolicyEntryV1) -> dict:
                before_read = current if now is not None else self._current()
                if current_entry != entry or not self._authority_current(
                    context, current_entry, before_read
                ):
                    return denied
                reader = AuthorizedCardSnapshotReader(
                    Path(home),
                    lambda candidate: decision if candidate == request else (_raise_policy()),
                    store_factory=self._store_factory,
                )
                result = reader.read(request, now=before_read)
                after_read = before_read if now is not None else self._current()
                return (
                    result
                    if self._authority_current(context, current_entry, after_read)
                    else denied
                )

            result = self._backend.read_if_current(
                selection,
                entry.owner_policy_revision,
                read_current,
            )
            if currentness_verifier.check_after_owner_read(context) is not DecisionState.ALLOW:
                return denied
            return result if isinstance(result, dict) else denied
        except Exception:
            return denied
        finally:
            if type(currentness_verifier) is ControlPlaneCurrentnessVerifier:
                currentness_verifier.close()

    def _resolve(
        self,
        binding: ControlPlaneBinding,
        capauth: AuthorizationDecision,
        now: datetime,
    ) -> AuthorizedCardPolicyEntryV1 | None:
        if not isinstance(binding, ControlPlaneBinding) or not isinstance(
            capauth, AuthorizationDecision
        ):
            return None
        acting = binding.agent_id or binding.principal.principal_id
        if (
            not capauth.allow
            or capauth.reason is not DecisionReason.ALLOW
            or capauth.attempt_sequence != 1
            or capauth.principal_id != binding.principal.principal_id
            or capauth.scope != binding.capability_scope()
            or capauth.credential_digest is None
            or capauth.trusted_issuer_policy_revision is None
            or not any(
                reference.principal_id == binding.principal.principal_id
                for reference in capauth.principal_policy_revisions
            )
            or capauth.revocation_revision is None
            or binding.purpose != "project-management-reporting"
            or binding.audience != "skdashboard"
            or binding.capability != "skdashboard.read"
            or binding.target != "/api/v1/overview"
            or binding.resource_type != "skcoord.card_store.project_snapshot"
            or binding.resource_id is None
            or now >= binding.expires_at
        ):
            return None
        selection = self._selection(binding)
        entry = self._backend.snapshot(selection)
        if not isinstance(entry, AuthorizedCardPolicyEntryV1):
            return None
        if (
            entry.subject != binding.principal.subject
            or entry.acting_principal_id != acting
            or entry.node_id != binding.node_id
            or entry.resource_id != binding.resource_id
            or entry.owner_policy_revision != binding.owner_policy_revision
            or not entry.valid_from <= now < entry.expires_at
        ):
            return None
        return entry

    @staticmethod
    def _selection(binding: ControlPlaneBinding) -> AuthorizedCardPolicySelectionV1:
        return AuthorizedCardPolicySelectionV1(
            subject=binding.principal.subject,
            acting_principal_id=binding.agent_id or binding.principal.principal_id,
            node_id=binding.node_id,
            resource_id=binding.resource_id or "",
        )

    def _authority_current(
        self,
        context: SanitizedControlPlaneDecisionV1,
        entry: AuthorizedCardPolicyEntryV1,
        current: datetime,
    ) -> bool:
        return (
            context.issued_at <= current < context.expires_at
            and entry.valid_from <= current < entry.expires_at
        )

    def _current(self, value: datetime | None = None) -> datetime:
        current = value if value is not None else self._clock()
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            raise ValueError("policy clock must use UTC offset zero")
        return current.astimezone(UTC)


def _raise_policy() -> AuthorizedCardSetDecisionV1:
    raise PermissionError("authorized card request binding mismatch")


__all__ = [
    "AuthorizedCardPolicyBackend",
    "AuthorizedCardPolicyEntryV1",
    "AuthorizedCardPolicyProvider",
    "AuthorizedCardPolicySelectionV1",
    "StaticAuthorizedCardPolicyBackend",
]
