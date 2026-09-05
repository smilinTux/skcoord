"""Authoritative, read-only fleet node and model-server discovery.

The adapter in this module only reads a caller-supplied JSON document. It has
no CMDB or registry handle and cannot apply its output. Runtime application is
intentionally a separate, reviewed operation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

FLEET_REGISTRY_SCHEMA_VERSION = 1
_SECRET_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "token",
    "api_key",
)


class RegistryState(str, Enum):
    """Discovery state, independent of service health or lifecycle state."""

    VALID = "valid"
    STALE = "stale"
    MISSING = "missing"


class Freshness(BaseModel):
    """Bounded observation evidence used to derive a record's state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: Optional[datetime] = None
    max_age_seconds: int = Field(default=21_600, gt=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_timestamp(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value


class _AuthoritativeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    node_binding: str = Field(min_length=1)
    endpoint: Optional[str] = None
    canonical_model_id: Optional[str] = None
    profile_revision: str = Field(min_length=1)
    capacity_domain: str = Field(min_length=1)
    freshness: Freshness
    ownership: str = Field(min_length=1)
    state: RegistryState = RegistryState.VALID

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_secret_free(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, query, or fragment")
        return value

    @model_validator(mode="after")
    def state_has_matching_evidence(self) -> "_AuthoritativeRecord":
        observed = self.freshness.observed_at
        if self.state == RegistryState.MISSING:
            if observed is not None or self.endpoint is not None:
                raise ValueError("missing records cannot claim observation or endpoint evidence")
        elif observed is None or self.endpoint is None:
            raise ValueError("non-missing records require endpoint and observed_at evidence")
        return self


class FleetNodeRecord(_AuthoritativeRecord):
    """Authoritative scheduler-visible node record."""

    kind: Literal["node"] = "node"
    canonical_model_id: None = None


class ModelServerRecord(_AuthoritativeRecord):
    """Authoritative capacity record bound to exactly one fleet node."""

    kind: Literal["model_server"] = "model_server"
    canonical_model_id: str = Field(min_length=1)


RegistryRecord = Union[FleetNodeRecord, ModelServerRecord]
_RECORD_ADAPTER = TypeAdapter(RegistryRecord)


class FleetRegistryDocument(BaseModel):
    """Versioned source document. Version defaults to 1 for older producers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = FLEET_REGISTRY_SCHEMA_VERSION
    records: tuple[dict[str, Any], ...] = ()


class RegistryDiscoveryError(ValueError):
    """The authoritative source is ambiguous or unsafe to consume."""


class ReadOnlyFleetRegistryDiscovery:
    """Parse and classify a registry source without mutating it or the CMDB."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def discover(
        self,
        *,
        now: Optional[datetime] = None,
        expected: Iterable[tuple[str, str]] = (),
    ) -> tuple[RegistryRecord, ...]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryDiscoveryError(f"cannot read registry source: {exc}") from exc
        _reject_secret_keys(raw)
        document = FleetRegistryDocument.model_validate(raw)

        records: list[RegistryRecord] = []
        identities: set[tuple[str, str]] = set()
        for item in document.records:
            record = _RECORD_ADAPTER.validate_python(item)
            identity = _identity(record)
            if identity in identities:
                raise RegistryDiscoveryError(f"duplicate authoritative record: {identity}")
            identities.add(identity)
            records.append(_classify(record, current))

        for kind, name in sorted(set(expected)):
            identity = (kind, name)
            if identity in identities:
                continue
            if kind == "node":
                records.append(
                    FleetNodeRecord(
                        node_binding=name,
                        profile_revision="missing",
                        capacity_domain="missing",
                        freshness=Freshness(),
                        ownership="unassigned",
                        state=RegistryState.MISSING,
                    )
                )
            elif kind == "model_server":
                node_binding, separator, model_id = name.partition("/")
                if not separator or not node_binding or not model_id:
                    raise RegistryDiscoveryError(
                        "expected model_server identity must be '<node_binding>/<canonical_model_id>'"
                    )
                records.append(
                    ModelServerRecord(
                        node_binding=node_binding,
                        canonical_model_id=model_id,
                        profile_revision="missing",
                        capacity_domain="missing",
                        freshness=Freshness(),
                        ownership="unassigned",
                        state=RegistryState.MISSING,
                    )
                )
            else:
                raise RegistryDiscoveryError(f"unknown expected record kind: {kind}")

        return tuple(sorted(records, key=_sort_key))


def _classify(record: RegistryRecord, now: datetime) -> RegistryRecord:
    if record.state == RegistryState.MISSING:
        return record
    observed = record.freshness.observed_at
    assert observed is not None
    state = (
        RegistryState.STALE
        if now.astimezone(timezone.utc) - observed.astimezone(timezone.utc)
        > timedelta(seconds=record.freshness.max_age_seconds)
        else RegistryState.VALID
    )
    return record.model_copy(update={"state": state.value})


def _identity(record: RegistryRecord) -> tuple[str, str]:
    if record.kind == "node":
        return (record.kind, record.node_binding)
    return (record.kind, f"{record.node_binding}/{record.canonical_model_id}")


def _sort_key(record: RegistryRecord) -> tuple[str, str]:
    return _identity(record)


def _reject_secret_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
                raise RegistryDiscoveryError(f"secret-bearing field is forbidden: {path}.{key}")
            _reject_secret_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{path}[{index}]")
