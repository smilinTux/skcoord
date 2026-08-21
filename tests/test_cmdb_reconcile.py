"""Tests for bounded CMDB orchestration, lifecycle, drift, and artifacts."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcoord.cmdb import CIStatus, CIType, CMDBManager, make_ci_id
from skcoord.cmdb_reconcile import (
    OrchestrationConfig,
    ScanResult,
    Target,
    TargetResult,
    apply_retirement_lifecycle,
    freshness_status,
    normalize_drift,
    operator_summary,
    resolve_targets,
    run_reconcile,
    scan_health_events,
    scan_network,
    write_run_artifact,
)
from skcoord.discovery import DISCOVERED_TAG, OBSERVED_COLLECTORS, DiscoveredCI, DriftFinding


class CannedRunner:
    """Minimal command runner whose host facts collector succeeds."""

    def __init__(self, host: str) -> None:
        self.host = host

    def run(self, argv: list[str]) -> str:
        if argv[:1] == ["uname"]:
            return "Linux 6.1\n"
        if argv[:1] == ["nproc"]:
            return "4\n"
        if argv[:2] == ["cat", "/proc/sys/net/ipv4/ip_local_port_range"]:
            return "32768 60999\n"
        return ""


def test_resolve_targets_deduplicates_and_preserves_provenance(tmp_path: Path) -> None:
    nodes = tmp_path / "fleet" / "objects" / "node"
    nodes.mkdir(parents=True)
    (nodes / "nor.json").write_text(
        json.dumps({"name": "nor", "spec": {"address": {"hostname": "nor.local"}}})
    )
    targets = resolve_targets(tmp_path, {"operator-list": ["nor.local", "chi.local"]})
    assert [target.host for target in targets] == ["chi.local", "nor.local"]
    assert targets[1].provenance == ("approved:operator-list", "fleet:node:nor")


def test_network_scan_reports_complete_target_accounting(tmp_path: Path) -> None:
    result = scan_network(
        tmp_path,
        [Target("nor.local", ("fleet:node:nor",))],
        CannedRunner,
        OrchestrationConfig(global_concurrency=2, per_host_concurrency=1, deadline_seconds=2),
    )
    assert result.complete
    assert result.completeness()["collectors_expected"] == len(OBSERVED_COLLECTORS)
    assert result.completeness()["collectors_complete"] == len(OBSERVED_COLLECTORS)
    assert any(item.name == "nor.local" for item in result.discovered)


def test_network_deadline_marks_run_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import skcoord.cmdb_reconcile as module

    def slow(_runner: CannedRunner) -> list[DiscoveredCI]:
        time.sleep(0.1)
        return []

    monkeypatch.setattr(module, "OBSERVED_COLLECTORS", (slow,))
    result = scan_network(
        tmp_path,
        [Target("nor", ("approved:test",))],
        CannedRunner,
        OrchestrationConfig(deadline_seconds=0.01),
    )
    assert not result.complete
    assert result.deadline_exceeded
    assert result.targets[0].failures == ["slow:deadline"]


def test_transport_failure_cannot_be_a_complete_empty_scan(tmp_path: Path) -> None:
    class DeadRunner:
        host = "dead"

        def run(self, _argv):
            return None

    result = scan_network(tmp_path, [Target("dead", ("fleet",))], lambda _host: DeadRunner())
    assert not result.complete
    assert result.completeness()["targets_complete"] == 0
    assert all("transport_unavailable" in failure for failure in result.targets[0].failures)


def test_tool_gaps_are_explicit_coverage_without_command_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import skcoord.cmdb_reconcile as module

    class MixedRunner:
        host = "mixed"

        def run(self, argv):
            if argv == ["true"] or argv == ["available"]:
                return ""
            return None

    def mixed(runner: MixedRunner) -> list[DiscoveredCI]:
        runner.run(["available"])
        runner.run(["missing", "--token", "must-not-survive"])
        return []

    monkeypatch.setattr(module, "OBSERVED_COLLECTORS", (mixed,))
    result = scan_network(tmp_path, [Target("mixed", ("fleet",))], lambda _host: MixedRunner())

    assert result.complete
    assert result.completeness()["collectors_partial"] == 1
    coverage = result.targets[0].coverage[0]
    assert coverage.status == "partial"
    assert coverage.commands_attempted == 2
    assert coverage.commands_succeeded == 1
    assert coverage.commands_unavailable == 1
    assert "token" not in json.dumps(coverage.__dict__)
    assert "must-not-survive" not in json.dumps(coverage.__dict__)


def test_fully_unavailable_collector_makes_target_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import skcoord.cmdb_reconcile as module

    class UnavailableRunner:
        host = "partial"

        def run(self, argv):
            return "" if argv == ["true"] else None

    def unavailable(runner: UnavailableRunner) -> list[DiscoveredCI]:
        runner.run(["missing"])
        return []

    monkeypatch.setattr(module, "OBSERVED_COLLECTORS", (unavailable,))
    result = scan_network(
        tmp_path,
        [Target("partial", ("fleet",))],
        lambda _host: UnavailableRunner(),
    )

    assert not result.complete
    assert result.completeness()["collectors_unavailable"] == 1
    assert result.targets[0].coverage[0].status == "unavailable"


def test_partial_scan_suppresses_absence_drift_and_deduplicates() -> None:
    findings = [
        DriftFinding("ci-service-a", "declared_not_observed", "missing"),
        DriftFinding("ci-service-a", "declared_not_observed", "missing"),
        DriftFinding("ci-service-b", "observed_not_declared", "unexpected"),
    ]
    normalized = normalize_drift(findings, "scan-1", False, "artifact.json")
    assert [(item.ci_id, item.kind) for item in normalized] == [
        ("ci-service-b", "observed_not_declared")
    ]
    assert normalized[0].event_type == "cmdb.drift"
    assert len(normalized[0].dedup_key) == 64


def test_scan_failure_is_health_not_asset_drift() -> None:
    scan = ScanResult([], [TargetResult("nor", ("fleet",), 1, failures=["ssh:timeout"])])
    assert not scan.complete
    assert (
        normalize_drift(
            [DriftFinding("ci-host-nor", "stored_not_discovered", "not seen")],
            "scan-1",
            scan.complete,
            "artifact.json",
        )
        == []
    )
    health = scan_health_events(scan, "scan-1", "artifact.json")
    assert health[0].event_type == "cmdb.scan_health"
    assert health[0].target == "nor"
    assert health[0].collector == "ssh"


def _owned_ci(mgr: CMDBManager, name: str = "gone") -> str:
    ci_id = make_ci_id(CIType.SERVICE.value, name)
    mgr.create_ci(name, tags=[DISCOVERED_TAG], ci_id=ci_id)
    return ci_id


def test_lifecycle_requires_complete_pass_and_retires_at_threshold(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci_id = _owned_ci(mgr)
    scope = "a" * 64
    assert apply_retirement_lifecycle(
        mgr, "systemd:nor", scope, [], [ci_id], False, apply=True
    ) == []
    assert "discovery_misses:systemd:nor" not in mgr.get_ci(ci_id).attributes
    apply_retirement_lifecycle(
        mgr, "systemd:nor", scope, [], [ci_id], True, threshold=2, apply=True
    )
    preview = apply_retirement_lifecycle(
        mgr, "systemd:nor", scope, [], [ci_id], True, threshold=2, apply=False
    )
    assert preview[0]["action"] == "retire"
    assert preview[0]["ci_id"] == ci_id
    apply_retirement_lifecycle(
        mgr, "systemd:nor", scope, [], [ci_id], True, threshold=2, apply=True
    )
    assert mgr.get_ci(ci_id).status == CIStatus.RETIRED.value


def test_lifecycle_reset_does_not_unretire(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci_id = _owned_ci(mgr)
    scope = "b" * 64
    mgr.set_attribute(ci_id, "test", f"discovery_misses:systemd:nor:{scope}", 2)
    mgr.set_status(ci_id, "human", CIStatus.RETIRED.value, note="manual")
    action = apply_retirement_lifecycle(
        mgr, "systemd:nor", scope, [ci_id], [ci_id], True, apply=True
    )[0]
    assert action["action"] == "reset"
    assert mgr.get_ci(ci_id).attributes[f"discovery_misses:systemd:nor:{scope}"] == 0
    assert mgr.get_ci(ci_id).status == CIStatus.RETIRED.value


def test_lifecycle_reports_dependents_before_retirement(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    target = _owned_ci(mgr, "database")
    dependent = _owned_ci(mgr, "api")
    mgr.add_relationship(dependent, "test", "depends_on", target)
    scope = "c" * 64
    mgr.set_attribute(target, "test", f"discovery_misses:test:{scope}", 2)
    preview = apply_retirement_lifecycle(
        mgr, "test", scope, [], [target], True, threshold=3, apply=False
    )[0]
    assert preview["action"] == "retire"
    assert preview["dependents"][0]["id"] == dependent


def test_artifact_is_canonical_checksummed_and_secret_safe(tmp_path: Path) -> None:
    path, checksum = write_run_artifact(
        tmp_path,
        {"scan_id": "run-1", "target": {"host": "nor", "password": "never-store-me"}},
    )
    assert path.name == "run-1.json"
    assert "never-store-me" not in path.read_text()
    assert '"password":"[redacted]"' in path.read_text()
    assert path.with_suffix(".sha256").read_text().startswith(checksum)


def test_artifact_rejects_path_like_scan_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_run_artifact(tmp_path, {"scan_id": "../escape"})


def test_freshness_slo_handles_missing_fresh_and_stale() -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert not freshness_status(None, now, timedelta(hours=4))["fresh"]
    assert freshness_status(now - timedelta(hours=3), now, timedelta(hours=4))["fresh"]
    assert not freshness_status(now - timedelta(hours=5), now, timedelta(hours=4))["fresh"]


def test_operator_summary_surfaces_drift_partial_runs_and_freshness() -> None:
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    artifacts = [
        {
            "scan_id": "partial",
            "ended_at": (now - timedelta(hours=1)).isoformat(),
            "completeness": {"complete": False},
            "drift": {"count": 1, "by_severity": {"medium": 1}},
        },
        {
            "scan_id": "complete",
            "ended_at": (now - timedelta(hours=2)).isoformat(),
            "completeness": {"complete": True},
        },
    ]
    summary = operator_summary(artifacts, now, timedelta(hours=3))
    assert summary["latest_scan_id"] == "partial"
    assert summary["latest_drift"]["count"] == 1
    assert summary["recent_failed_or_partial"] == 1
    assert summary["freshness"]["fresh"]


def test_run_artifact_counts_retirement_actions(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    scan = ScanResult([], [TargetResult("nor", ("fleet",), 0)])
    artifact, _ = run_reconcile(
        mgr,
        scan,
        lifecycle_actions=[{"ci_id": "ci-service-gone", "action": "retire"}],
    )
    assert artifact["reconcile"]["retired"] == ["ci-service-gone"]
    assert artifact["reconcile"]["counts"]["retired"] == 1


def test_partial_run_never_reports_orphans(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    _owned_ci(mgr, "old")
    scan = ScanResult(
        [],
        [TargetResult("dead", ("fleet",), 1, failures=["probe:transport_unavailable"])],
    )
    artifact, _ = run_reconcile(mgr, scan)
    assert artifact["completeness"]["complete"] is False
    assert artifact["reconcile"]["orphans"] == []


def test_applied_reconcile_enrolls_ci_in_exact_lifecycle_scope(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    item = DiscoveredCI("host", "nor", "ssh", observed=True, authority="network:nor")
    scan = ScanResult([item], [TargetResult("nor", ("fleet",), 1, completed_collectors=1)])

    artifact, _ = run_reconcile(mgr, scan, apply=True)

    ci = mgr.get_ci(item.ci_id)
    assert ci is not None
    assert ci.attributes["lifecycle_scope"] == scan.scope_fingerprint()
    assert artifact["scope_fingerprint"] == scan.scope_fingerprint()


def test_scope_fingerprint_changes_for_same_count_different_collectors() -> None:
    target = TargetResult("nor", ("fleet",), 2)
    first = ScanResult([], [target], collector_scope=("collect.a", "collect.b"))
    second = ScanResult([], [target], collector_scope=("collect.a", "collect.c"))

    assert first.scope_fingerprint() != second.scope_fingerprint()
