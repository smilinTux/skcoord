"""Tests for scheduled CMDB reconciliation policy and evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcoord.cmdb_reconcile import write_run_artifact
from skcoord.cmdb_scheduler import (
    ReconcileLease,
    ScheduledReconcileConfig,
    consecutive_event_count,
    load_reconcile_job_config,
    prune_run_artifacts,
    route_reconcile_incidents,
)


def _config(**overrides) -> ScheduledReconcileConfig:
    values = {
        "enabled": True,
        "targets": ("chiap04",),
        "credential_refs": {"chiap04": "skvault://fleet/chiap04"},
    }
    values.update(overrides)
    return ScheduledReconcileConfig(**values)


def _artifact(ended_at: str, group: str, event: dict) -> dict:
    return {"ended_at": ended_at, "events": {group: [event]}}


def test_missing_config_is_disabled_and_side_effect_free(tmp_path: Path) -> None:
    config = load_reconcile_job_config(tmp_path / "missing.json")
    assert not config.enabled
    assert config.owner_node == "chiap04"
    assert not (tmp_path / "scheduler").exists()


def test_enabled_config_requires_exact_skvault_mapping() -> None:
    with pytest.raises(ValueError, match="one credential reference"):
        ScheduledReconcileConfig(enabled=True, targets=("chiap04",))
    with pytest.raises(ValueError, match="skvault"):
        ScheduledReconcileConfig(
            enabled=True,
            targets=("chiap04",),
            credential_refs={"chiap04": "ssh://inline"},
        )


def test_config_rejects_truthy_strings_and_string_target_lists() -> None:
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        ScheduledReconcileConfig.from_mapping({"enabled": "false"})
    with pytest.raises(ValueError, match="targets must be an array"):
        ScheduledReconcileConfig.from_mapping({"targets": "chiap04"})


def test_config_round_trip_and_fingerprint(tmp_path: Path) -> None:
    config = _config(retention_runs=12, failure_alert_runs=4)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.as_dict()), encoding="utf-8")
    loaded = load_reconcile_job_config(path)
    assert loaded == config
    assert loaded.fingerprint() == config.fingerprint()
    assert len(loaded.fingerprint()) == 64


def test_reconcile_lease_allows_exactly_one_holder(tmp_path: Path) -> None:
    with ReconcileLease(tmp_path, "chiap04", "jarvis") as first:
        assert first.acquired
        with ReconcileLease(tmp_path, "chiap04", "jarvis") as second:
            assert not second.acquired
    with ReconcileLease(tmp_path, "chiap04", "jarvis") as next_run:
        assert next_run.acquired


def test_consecutive_event_count_stops_at_clear_run() -> None:
    event = {"dedup_key": "same"}
    artifacts = [
        _artifact("2026-08-22T03:00:00+00:00", "scan_health", event),
        _artifact("2026-08-22T02:00:00+00:00", "scan_health", event),
        {"ended_at": "2026-08-22T01:00:00+00:00", "events": {"scan_health": []}},
        _artifact("2026-08-22T00:00:00+00:00", "scan_health", event),
    ]
    assert consecutive_event_count(artifacts, "scan_health", "same") == 2


def test_material_drift_and_repeated_failure_create_deduplicated_incidents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeITIL:
        incidents: dict[str, SimpleNamespace] = {}

        def __init__(self, _home: Path) -> None:
            pass

        def find_open_incident_for_service(self, service: str):
            return self.incidents.get(service)

        def create_incident(self, title: str, **kwargs):
            service = kwargs["affected_services"][0]
            incident = SimpleNamespace(id=f"inc-{len(self.incidents) + 1}", title=title)
            self.incidents[service] = incident
            return incident

    monkeypatch.setattr("skcoord.cmdb_scheduler.ITILManager", FakeITIL)
    config = _config(failure_alert_runs=2, drift_alert_runs=3)
    drift = {
        "dedup_key": "drift-key",
        "ci_id": "ci-service-api",
        "kind": "declared_not_observed",
        "severity": "high",
        "detail": "missing",
    }
    failure = {
        "dedup_key": "failure-key",
        "target": "chiap04",
        "collector": "systemd",
        "failure": "timeout",
    }
    newest = {
        "ended_at": "2026-08-22T03:00:00+00:00",
        "events": {"drift": [drift], "scan_health": [failure]},
    }
    older = {
        "ended_at": "2026-08-22T02:00:00+00:00",
        "events": {"drift": [], "scan_health": [failure]},
    }

    first = route_reconcile_incidents(tmp_path, [newest, older], config)
    second = route_reconcile_incidents(tmp_path, [newest, older], config)

    assert first == ["inc-1", "inc-2"]
    assert second == first
    assert len(FakeITIL.incidents) == 2


def test_retention_removes_oldest_pairs_and_records_receipt(tmp_path: Path) -> None:
    paths = []
    for index in range(3):
        path, _ = write_run_artifact(tmp_path, {"scan_id": f"run-{index}"})
        path.touch()
        paths.append(path)

    removed = prune_run_artifacts(tmp_path, 2)

    assert removed == [paths[0]]
    assert not paths[0].exists()
    assert not paths[0].with_suffix(".sha256").exists()
    assert (tmp_path / "cmdb" / "reconcile-retention-last.json").exists()
