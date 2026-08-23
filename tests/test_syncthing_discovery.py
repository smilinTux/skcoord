"""Tests for bounded, secret-free Syncthing CMDB discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcoord.cmdb import CIStatus, CIType, is_secret_attribute_key, make_ci_id
from skcoord.cmdb_reconcile import OrchestrationConfig, Target, scan_network
from skcoord.discovery import _observed_status
from skcoord.discovery_syncthing import collect_syncthing_health


class ProbeRunner:
    def __init__(self, host: str, payload: object) -> None:
        self.host = host
        self.payload = payload

    def run(self, _argv) -> str:
        return json.dumps(self.payload)


def healthy_payload() -> dict:
    return {
        "schema": 1,
        "configured": True,
        "available": True,
        "version": "v2.1.3",
        "config_schema": 52,
        "system_errors": 0,
        "connected_devices": 9,
        "ports": [8384, 22000],
        "folders": [
            {
                "id": "skcapstone",
                "state": "idle",
                "paused": False,
                "pending_items": 0,
                "pull_errors": 0,
            }
        ],
    }


def test_healthy_probe_creates_qualified_service_ports_and_edges() -> None:
    items = collect_syncthing_health(ProbeRunner("chiap04", healthy_payload()))
    service = items[0]

    assert service.name == "syncthing@chiap04"
    assert service.ci_id == make_ci_id(CIType.SERVICE.value, service.name)
    assert service.attributes["sync_health_state"] == "healthy"
    assert service.attributes["sync_folders"] == [
        {
            "folder_id": "skcapstone",
            "state": "idle",
            "paused": False,
            "pending_items": 0,
            "pull_errors": 0,
        }
    ]
    assert _observed_status(service) == CIStatus.OPERATIONAL.value
    assert {item.name for item in items[1:]} == {"chiap04:8384", "chiap04:22000"}
    assert set(service.relationships) == {
        ("runs_on", make_ci_id(CIType.HOST.value, "chiap04")),
        ("connects_to", make_ci_id(CIType.PORT.value, "chiap04:8384")),
        ("connects_to", make_ci_id(CIType.PORT.value, "chiap04:22000")),
    }


def test_fleet_nodes_do_not_collapse_to_one_service() -> None:
    first = collect_syncthing_health(ProbeRunner("chiap01", healthy_payload()))[0]
    second = collect_syncthing_health(ProbeRunner("chiap02", healthy_payload()))[0]
    assert first.ci_id != second.ci_id


@pytest.mark.parametrize(
    (
        "available",
        "system_errors",
        "folder_state",
        "pending",
        "pull_errors",
        "health",
        "status",
    ),
    [
        (False, 0, "unknown", 0, 0, "down", CIStatus.DOWN.value),
        (True, 1, "idle", 0, 0, "degraded", CIStatus.DEGRADED.value),
        (True, 0, "idle", 0, 2, "degraded", CIStatus.DEGRADED.value),
        (True, 0, "syncing", 3, 0, "syncing", CIStatus.OPERATIONAL.value),
    ],
)
def test_health_and_cmdb_status_mapping(
    available, system_errors, folder_state, pending, pull_errors, health, status
) -> None:
    payload = healthy_payload()
    payload.update(available=available, system_errors=system_errors)
    payload["folders"][0].update(
        state=folder_state, pending_items=pending, pull_errors=pull_errors
    )
    service = collect_syncthing_health(ProbeRunner("node", payload))[0]
    assert service.attributes["sync_health_state"] == health
    assert _observed_status(service) == status


def test_collector_evidence_contains_no_secret_keys_or_local_paths() -> None:
    items = collect_syncthing_health(ProbeRunner("node", healthy_payload()))

    def keys(value: object):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from keys(child)

    assert all(
        not is_secret_attribute_key(key)
        for item in items
        for key in keys(item.attributes)
    )
    serialized = json.dumps([item.attributes for item in items])
    assert "/home/" not in serialized
    assert "config.xml" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"schema": 999, "configured": True},
        {**healthy_payload(), "folders": healthy_payload()["folders"] * 33},
        {**healthy_payload(), "ports": "8384"},
    ],
)
def test_invalid_or_unbounded_probe_payload_is_rejected(payload: object) -> None:
    class RawRunner:
        host = "node"

        def run(self, _argv):
            return payload if isinstance(payload, str) else json.dumps(payload)

    with pytest.raises(ValueError):
        collect_syncthing_health(RawRunner())


def test_unavailable_probe_makes_scheduled_scan_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import skcoord.cmdb_reconcile as module

    class UnavailableRunner:
        host = "node"

        def run(self, argv):
            return "" if argv == ["true"] else None

    monkeypatch.setattr(module, "OBSERVED_COLLECTORS", (collect_syncthing_health,))
    result = scan_network(
        tmp_path,
        [Target("node", ("approved:test",))],
        lambda _host: UnavailableRunner(),
        OrchestrationConfig(deadline_seconds=2),
    )

    assert not result.complete
    assert result.targets[0].coverage[0].status == "unavailable"
    assert result.discovered == []
