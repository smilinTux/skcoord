"""Tests for CMDB discovery.

The collectors are only worth having if a *failed* collector looks different
from an empty fleet, so most of these tests are negative controls: a runner
that returns nothing must produce nothing, and a reconcile that cannot see a
CI must not delete it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcoord.cmdb import CIStatus, CIType, CMDBManager, make_ci_id
from skcoord.discovery import (
    DISCOVERED_TAG,
    DiscoveredCI,
    collect_agents,
    collect_fleet_objects,
    collect_host_facts,
    collect_listening_ports,
    collect_registry,
    collect_systemd_units,
    drift,
    merge,
    reconcile,
    scan,
)

SS_OUTPUT = """LISTEN 0      4096                     127.0.0.1:11434 0.0.0.0:*
LISTEN 0      5                        127.0.0.1:9400  0.0.0.0:* users:(("skcapstone",pid=4121014,fd=13))
LISTEN 0      4096                 127.0.0.53%lo:53    0.0.0.0:*
"""

SYSTEMD_OUTPUT = """skgateway.service           loaded active   running SKGateway router
skchat-daemon.service       loaded active   running SKChat daemon
dead-thing.service          loaded failed   failed  Something broken
"""


class FakeRunner:
    """A runner with canned answers, so collectors are testable off-fleet."""

    def __init__(self, host: str = "testnode", answers: dict | None = None) -> None:
        self.host = host
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        for key, value in self.answers.items():
            if key in " ".join(argv):
                return value
        return None


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    (tmp_path / "fleet" / "objects" / "node").mkdir(parents=True)
    (tmp_path / "fleet" / "objects" / "service").mkdir(parents=True)
    (tmp_path / "fleet" / "objects" / "cronjob").mkdir(parents=True)
    (tmp_path / "fleet" / "objects" / "operatorapp").mkdir(parents=True)
    (tmp_path / "registry").mkdir()
    (tmp_path / "agents" / "lumina" / "soul").mkdir(parents=True)

    (tmp_path / "fleet" / "objects" / "node" / "node-alpha.json").write_text(
        json.dumps(
            {
                "kind": "Node",
                "name": "node-alpha",
                "generation": 6,
                "spec": {
                    "address": {"hostname": "alpha01"},
                    "role": "control-plane",
                    "cordoned": False,
                },
            }
        )
    )
    (tmp_path / "fleet" / "objects" / "service" / "skgateway.json").write_text(
        json.dumps(
            {
                "kind": "Service",
                "name": "skgateway",
                "labels": {"tier": "core"},
                "spec": {"runtime": "systemd", "note": "model router"},
            }
        )
    )
    (tmp_path / "registry" / "skgateway.json").write_text(
        json.dumps(
            {
                "name": "skgateway",
                "health_url": "http://localhost:18991/health",
                "registered_at": "2026-08-17T02:39:29.636Z",
            }
        )
    )
    (tmp_path / "agents" / "lumina" / "soul" / "base.json").write_text(
        json.dumps({"name": "Lumina", "role": "devops"})
    )
    return tmp_path


# ── declared collectors ───────────────────────────────────────────────────


def test_fleet_objects_yield_host_and_service_cis(home: Path) -> None:
    found = collect_fleet_objects(home)
    hosts = [c for c in found if c.ci_type == CIType.HOST.value]
    services = [c for c in found if c.ci_type == CIType.SERVICE.value]

    assert [h.name for h in hosts] == ["alpha01"]
    assert hosts[0].attributes["role"] == "control-plane"
    assert [s.name for s in services] == ["skgateway"]
    assert all(not c.observed for c in found), "specs are declarations, never observations"


def test_registry_entries_stay_declared_with_their_timestamp(home: Path) -> None:
    found = collect_registry(home)
    assert len(found) == 1
    entry = found[0]
    assert entry.observed is False, "a registry file is a claim, not a live probe"
    assert entry.attributes["registered_at"].startswith("2026-08-17")


def test_agents_are_collected_with_soul_facts(home: Path) -> None:
    found = collect_agents(home)
    assert [a.name for a in found] == ["lumina"]
    assert found[0].attributes["soul_role"] == "devops"


def test_declared_collectors_are_empty_on_an_empty_home(tmp_path: Path) -> None:
    assert collect_fleet_objects(tmp_path) == []
    assert collect_registry(tmp_path) == []
    assert collect_agents(tmp_path) == []


# ── observed collectors ───────────────────────────────────────────────────


def test_systemd_units_are_observed_and_bound_to_their_host() -> None:
    runner = FakeRunner(answers={"list-units": SYSTEMD_OUTPUT})
    found = collect_systemd_units(runner, scopes=("--user",))

    names = {c.name for c in found}
    assert names == {"skgateway", "skchat-daemon", "dead-thing"}
    assert all(c.observed for c in found)
    gateway = next(c for c in found if c.name == "skgateway")
    assert gateway.attributes["active_state"] == "active"
    assert ("runs_on", make_ci_id(CIType.HOST.value, "testnode")) in gateway.relationships

    failed = next(c for c in found if c.name == "dead-thing")
    assert failed.attributes["active_state"] == "failed", "a failed unit is still an asset"


def test_listening_ports_parse_bind_port_and_process() -> None:
    runner = FakeRunner(answers={"ss": SS_OUTPUT})
    found = collect_listening_ports(runner)

    ports = {c.attributes["port"]: c for c in found}
    assert set(ports) == {11434, 9400, 53}
    assert ports[9400].attributes["process"] == "skcapstone"
    assert ports[53].attributes["bind"] == "127.0.0.53%lo"
    assert all(c.observed for c in found)


def test_observed_collectors_return_nothing_when_the_host_is_unreachable() -> None:
    """A dead runner must look like a dead runner, not like a clean machine."""
    runner = FakeRunner(answers={})
    assert collect_systemd_units(runner) == []
    assert collect_listening_ports(runner) == []
    assert collect_host_facts(runner) == []


# ── scan + merge ──────────────────────────────────────────────────────────


def test_scan_without_runners_collects_no_observed_state(home: Path) -> None:
    found = scan(home)
    assert found, "declared sources should still be collected"
    assert not any(c.observed for c in found), "no runner means nothing was observed"


def test_scan_never_invents_a_hostname(home: Path) -> None:
    """The bug this module replaces: hardcoded noroc2027/.41/.100."""
    found = scan(home)
    names = {c.name for c in found}
    assert not {"noroc2027", "comfyui", "cbrd21-laptop12thgenintelcore"} & names


def test_observation_beats_declaration_when_merging() -> None:
    declared = DiscoveredCI(
        ci_type="service",
        name="skgateway",
        source="fleet:service",
        description="from spec",
        attributes={"tier": "core", "active_state": "unknown"},
        tags=("fleet",),
    )
    observed = DiscoveredCI(
        ci_type="service",
        name="skgateway",
        source="systemd--user",
        observed=True,
        node="alpha01",
        attributes={"active_state": "active"},
        tags=("systemd",),
    )

    folded = merge([declared, observed])
    assert len(folded) == 1
    ci = folded[0]
    assert ci.observed is True
    assert ci.node == "alpha01"
    assert ci.attributes["active_state"] == "active", "the live probe wins"
    assert ci.attributes["tier"] == "core", "declaration-only facts survive"
    assert set(ci.tags) == {"fleet", "systemd"}
    assert ci.source == "fleet:service+systemd--user"


def test_merge_is_order_independent() -> None:
    declared = DiscoveredCI("service", "x", "spec", attributes={"a": 1})
    observed = DiscoveredCI("service", "x", "live", observed=True, attributes={"a": 2})
    assert merge([declared, observed])[0].attributes["a"] == 2
    assert merge([observed, declared])[0].attributes["a"] == 2


# ── reconcile ─────────────────────────────────────────────────────────────


def test_reconcile_dry_run_writes_nothing(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    found = [DiscoveredCI("service", "skgateway", "fleet:service", tags=(DISCOVERED_TAG,))]

    report = reconcile(mgr, found, apply=False)

    assert report.created == [make_ci_id("service", "skgateway")]
    assert report.applied is False
    assert mgr.list_cis() == [], "a dry run must not touch the store"


def test_reconcile_apply_creates_cis_and_relationships(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    host_id = make_ci_id(CIType.HOST.value, "alpha01")
    found = [
        DiscoveredCI(
            "service",
            "skgateway",
            "systemd",
            observed=True,
            node="alpha01",
            attributes={"active_state": "active"},
            tags=(DISCOVERED_TAG,),
            relationships=(("runs_on", host_id),),
        )
    ]

    reconcile(mgr, found, apply=True)

    stored = mgr.list_cis()
    assert [c.id for c in stored] == [make_ci_id("service", "skgateway")]
    assert stored[0].attributes["active_state"] == "active"
    assert [(r.rel_type, r.target) for r in stored[0].relationships] == [("runs_on", host_id)]


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    found = [
        DiscoveredCI(
            "service", "skgateway", "systemd", attributes={"port": 18991}, tags=(DISCOVERED_TAG,)
        )
    ]

    reconcile(mgr, found, apply=True)
    second = reconcile(mgr, found, apply=True)

    assert second.created == []
    assert second.updated == {}
    assert second.unchanged == [make_ci_id("service", "skgateway")]


def test_reconcile_records_changed_attributes(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    before = [
        DiscoveredCI(
            "service", "skgateway", "systemd", attributes={"active_state": "active"},
            tags=(DISCOVERED_TAG,),
        )
    ]
    reconcile(mgr, before, apply=True)

    after = [
        DiscoveredCI(
            "service", "skgateway", "systemd", attributes={"active_state": "failed"},
            tags=(DISCOVERED_TAG,),
        )
    ]
    report = reconcile(mgr, after, apply=True)

    ci_id = make_ci_id("service", "skgateway")
    assert report.updated == {ci_id: ["active_state"]}
    assert mgr.get_ci(ci_id).attributes["active_state"] == "failed"


def test_reconcile_never_deletes_a_ci_it_cannot_see(tmp_path: Path) -> None:
    """A collector that silently fails must not be able to erase inventory."""
    mgr = CMDBManager(tmp_path)
    reconcile(
        mgr,
        [DiscoveredCI("service", "skgateway", "systemd", tags=(DISCOVERED_TAG,))],
        apply=True,
    )

    report = reconcile(mgr, [], apply=True)

    assert report.orphans == [make_ci_id("service", "skgateway")]
    assert len(mgr.list_cis()) == 1, "orphans are reported, never removed"


def test_reconcile_ignores_hand_made_cis_when_reporting_orphans(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("hand-made", "service", tags=["manual"])

    report = reconcile(mgr, [], apply=True)

    assert report.orphans == [], "only discovery-owned CIs can be orphaned by discovery"


def test_reconcile_skips_already_retired_orphans(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci("gone", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci.id, "op", CIStatus.RETIRED.value)

    assert reconcile(mgr, [], apply=True).orphans == []


# ── drift ─────────────────────────────────────────────────────────────────


def test_drift_flags_a_service_declared_but_not_running() -> None:
    found = [
        DiscoveredCI("service", "skgateway", "fleet:service"),
        DiscoveredCI("service", "skchat-daemon", "systemd", observed=True),
        DiscoveredCI("service", "skchat-daemon", "fleet:service"),
    ]
    kinds = {f.ci_id: f.kind for f in drift(merge(found))}
    assert kinds[make_ci_id("service", "skgateway")] == "declared_not_observed"


def test_drift_flags_a_service_running_that_nothing_declared() -> None:
    found = [DiscoveredCI("service", "mystery-thing", "systemd", observed=True, node="alpha01")]
    findings = drift(found)
    assert [f.kind for f in findings] == ["observed_not_declared"]


def test_drift_matches_across_the_dot_service_suffix() -> None:
    """'skgateway' in a spec and 'skgateway.service' on the box are one thing."""
    found = [
        DiscoveredCI("service", "skgateway", "fleet:service"),
        DiscoveredCI("service", "skgateway.service", "systemd", observed=True),
    ]
    assert drift(found) == []


def test_drift_reports_stored_cis_no_collector_saw(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("decommissioned", "service", tags=[DISCOVERED_TAG])

    findings = drift([], mgr)

    assert [f.kind for f in findings] == ["stored_not_discovered"]
