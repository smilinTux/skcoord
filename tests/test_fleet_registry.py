import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from skcoord.fleet_registry import (
    ReadOnlyFleetRegistryDiscovery,
    RegistryDiscoveryError,
    RegistryState,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _record(**overrides):
    record = {
        "kind": "model_server",
        "node_binding": "chiap02",
        "endpoint": "https://models.internal:8443/v1",
        "canonical_model_id": "qwen/qwen3-coder-30b",
        "profile_revision": "profile-2026-09-05.1",
        "capacity_domain": "chiap02/gpu0",
        "freshness": {
            "observed_at": "2026-09-05T11:30:00Z",
            "max_age_seconds": 3600,
        },
        "ownership": "scheduler:jarvis",
        "state": "valid",
    }
    record.update(overrides)
    return record


def _source(path: Path, records, *, include_version: bool = True) -> Path:
    payload = {"records": records}
    if include_version:
        payload["schema_version"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_discovers_valid_records_deterministically_and_accepts_legacy_version_default(
    tmp_path: Path,
) -> None:
    node = {
        **_record(kind="node", canonical_model_id=None),
        "endpoint": "https://chiap02.internal:9443/status",
    }
    path = _source(tmp_path / "registry.json", [_record(), node], include_version=False)

    first = ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)
    second = ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)

    assert first == second
    assert [record.kind for record in first] == ["model_server", "node"]
    model = first[0]
    assert model.model_dump(mode="json") == {
        "node_binding": "chiap02",
        "endpoint": "https://models.internal:8443/v1",
        "canonical_model_id": "qwen/qwen3-coder-30b",
        "profile_revision": "profile-2026-09-05.1",
        "capacity_domain": "chiap02/gpu0",
        "freshness": {
            "observed_at": "2026-09-05T11:30:00Z",
            "max_age_seconds": 3600,
        },
        "ownership": "scheduler:jarvis",
        "state": "valid",
        "kind": "model_server",
    }


def test_derives_stale_from_freshness_not_declared_state(tmp_path: Path) -> None:
    path = _source(
        tmp_path / "registry.json",
        [_record(freshness={"observed_at": "2026-09-05T10:59:59Z", "max_age_seconds": 3600})],
    )

    records = ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)

    assert records[0].state == RegistryState.STALE


def test_emits_explicit_missing_records_for_expected_inventory(tmp_path: Path) -> None:
    path = _source(tmp_path / "registry.json", [])

    records = ReadOnlyFleetRegistryDiscovery(path).discover(
        now=NOW,
        expected={
            ("node", "chiap03"),
            ("model_server", "chiap03/meta/llama-3.3-70b"),
        },
    )

    assert [record.state for record in records] == ["missing", "missing"]
    assert records[0].canonical_model_id == "meta/llama-3.3-70b"
    assert records[1].node_binding == "chiap03"
    assert all(record.endpoint is None for record in records)


def test_rejects_duplicate_authoritative_identity(tmp_path: Path) -> None:
    path = _source(tmp_path / "registry.json", [_record(), _record(endpoint="https://other/v1")])

    with pytest.raises(RegistryDiscoveryError, match="duplicate authoritative record"):
        ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"api_token": "do-not-store"}, "secret-bearing field"),
        ({"endpoint": "https://user:password@models.internal/v1"}, "must not contain"),
        ({"endpoint": "https://models.internal/v1?token=abc"}, "must not contain"),
    ],
)
def test_rejects_secret_bearing_records(tmp_path: Path, mutation, message: str) -> None:
    path = _source(tmp_path / "registry.json", [_record(**mutation)])

    with pytest.raises((RegistryDiscoveryError, ValidationError), match=message):
        ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)


def test_discovery_is_read_only(tmp_path: Path) -> None:
    path = _source(tmp_path / "registry.json", [_record()])
    before = path.read_bytes()

    ReadOnlyFleetRegistryDiscovery(path).discover(now=NOW)

    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["registry.json"]
