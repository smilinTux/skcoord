"""Tests for CMDB discovery.

The collectors are only worth having if a *failed* collector looks different
from an empty fleet, so most of these tests are negative controls: a runner
that returns nothing must produce nothing, and a reconcile that cannot see a
CI must not delete it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcoord.cmdb import CIStatus, CIType, CMDBManager, make_ci_id
from skcoord.discovery import (
    DISCOVERED_TAG,
    ORIGIN_DISTRO,
    ORIGIN_OPERATOR,
    ORIGIN_UNKNOWN,
    DiscoveredCI,
    ObservationState,
    collect_agents,
    collect_docker_containers,
    collect_fleet_objects,
    collect_host_facts,
    collect_listening_ports,
    collect_registry,
    collect_systemd_units,
    device_from_fingerprint,
    drift,
    merge,
    observation_state,
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

TIMER_OUTPUT = """backup.timer                loaded active   waiting Nightly backup
"""

# 443 and 18991 are stable services; 40123 and 55281 are random high ports of
# the kind that change on every reboot.
SS_EPHEMERAL_OUTPUT = """LISTEN 0      4096                       0.0.0.0:443   0.0.0.0:*
LISTEN 0      4096                     127.0.0.1:18991 0.0.0.0:*
LISTEN 0      4096                       0.0.0.0:40123 0.0.0.0:*
LISTEN 0      4096                          [::]:55281 [::]:*
"""

NOT_FOUND_OUTPUT = """skgateway.service   loaded    active   running SKGateway router
connman.service     not-found inactive dead    connman.service
"""

SHOW_OUTPUT = """Id=skgateway.service
FragmentPath=/home/cbrd21/.config/systemd/user/skgateway.service

Id=skchat-daemon.service
FragmentPath=/usr/lib/systemd/user/skchat-daemon.service

Id=backup.timer
FragmentPath=/home/cbrd21/.config/systemd/user/backup.timer
"""

DOCKER_OUTPUT = (
    "skchat-coturn\tcoturn/coturn:4.6\tUp 9 days\t0.0.0.0:3478->3478/tcp\n"
    "skmem-pg\t43fc80538777\tUp 9 days\t\n"
)


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


def test_ephemeral_ports_are_skipped_so_the_cmdb_cannot_grow_without_bound() -> None:
    """The store is append-only and the scan runs every 3h. Random high ports
    would accrete forever, each reboot's set orphaning the last."""
    runner = FakeRunner(answers={"ss": SS_EPHEMERAL_OUTPUT, "ip_local_port_range": "32768\t60999\n"})
    found = collect_listening_ports(runner)

    ports = {c.attributes["port"] for c in found}
    assert ports == {443, 18991}, "only stable ports become assets"


def test_ephemeral_range_is_read_from_the_host_not_hardcoded() -> None:
    """A tuned node moves the range, and a remote scan must use the REMOTE
    host's range, not this machine's."""
    runner = FakeRunner(
        answers={"ss": SS_EPHEMERAL_OUTPUT, "ip_local_port_range": "18000\t19000\n"}
    )
    found = collect_listening_ports(runner)

    ports = {c.attributes["port"] for c in found}
    assert 18991 not in ports, "18991 is ephemeral on THIS host's tuned range"
    assert 443 in ports
    assert 40123 in ports, "outside the tuned range, so no longer ephemeral"


def test_ephemeral_range_falls_back_when_the_host_will_not_say() -> None:
    runner = FakeRunner(answers={"ss": SS_EPHEMERAL_OUTPUT})
    ports = {c.attributes["port"] for c in collect_listening_ports(runner)}
    assert ports == {443, 18991}


def test_observed_collectors_return_nothing_when_the_host_is_unreachable() -> None:
    """A dead runner must look like a dead runner, not like a clean machine."""
    runner = FakeRunner(answers={})
    assert collect_systemd_units(runner) == []
    assert collect_listening_ports(runner) == []
    assert collect_host_facts(runner) == []


def test_host_facts_normalise_linux_capacity_and_preserve_provenance() -> None:
    runner = FakeRunner(
        host="ssh-alpha",
        answers={
            "uname": "Linux 6.8\n",
            "nproc": "8\n",
            "lscpu": json.dumps({"lscpu": [
                {"field": "Socket(s):", "data": "1"},
                {"field": "Core(s) per socket:", "data": "4"},
                {"field": "Thread(s) per core:", "data": "2"},
                {"field": "Model name:", "data": "Pengu CPU"},
            ]}),
            "free": "              total used free shared buff/cache available\nMem: 1000 400 200 10 400 500\n",
            "ip -j": json.dumps([{"ifname": "eth0", "addr_info": [
                {"local": "192.0.2.10", "scope": "global"}
            ]}]),
            "lsblk": json.dumps({"blockdevices": [
                {"name": "sda", "type": "disk", "size": 10000, "children": []}
            ]}),
            "df": "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/sda1 9000 4000 5000 45% /\n",
        },
    )
    host = collect_host_facts(runner)[0]
    assert host.attributes["cpu_logical"] == 8
    assert host.attributes["cpu_cores_per_socket"] == 4
    assert host.attributes["memory_total_bytes"] == 1000
    assert host.attributes["disk_capacity_bytes"] == 10000
    assert host.attributes["filesystems"][0]["used_bytes"] == 4000
    assert host.attributes["ip_addresses"] == ["192.0.2.10"]
    assert "lscpu" in host.attributes["fact_provenance"]
    assert "192.0.2.10" in host.identity_aliases


def test_host_fact_failures_are_missing_not_zero() -> None:
    host = collect_host_facts(FakeRunner(answers={"uname": "Linux 6.8\n"}))[0]
    assert "memory_total_bytes" not in host.attributes
    assert "disk_capacity_bytes" not in host.attributes
    assert "cpu_logical" not in host.attributes


def test_alias_overlap_merges_host_sightings_under_declared_canonical_name() -> None:
    declared = DiscoveredCI(
        "host", "alpha.example", "fleet:node", canonical_name="alpha.example",
        aliases=("ssh-alpha", "192.0.2.10"),
    )
    observed = DiscoveredCI(
        "host", "ssh-alpha", "host", observed=True, canonical_name="ssh-alpha",
        aliases=("192.0.2.10",),
    )
    folded = merge([declared, observed])
    assert len(folded) == 1
    assert folded[0].ci_id == make_ci_id("host", "alpha.example")
    assert set(folded[0].identity_aliases) == {"alpha.example", "ssh-alpha", "192.0.2.10"}


def test_fingerprint_only_asset_is_an_unmanaged_device() -> None:
    device = device_from_fingerprint("printer", "arp", aliases=("192.0.2.50",))
    assert device.ci_type == CIType.DEVICE.value
    assert device.attributes["managed"] is False
    assert "unmanaged" in device.tags


def test_observation_freshness_is_not_health() -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert observation_state("") is ObservationState.UNKNOWN
    assert observation_state((now - timedelta(hours=1)).isoformat(), now=now) is ObservationState.FRESH
    assert observation_state((now - timedelta(days=1)).isoformat(), now=now) is ObservationState.STALE


# ── scan + merge ──────────────────────────────────────────────────────────


def test_scan_without_runners_collects_no_observed_state(home: Path) -> None:
    found = scan(home)
    assert found, "declared sources should still be collected"
    assert not any(c.observed for c in found), "no runner means nothing was observed"
    assert all(c.scan_id and c.authority == "declared" for c in found)


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


def test_a_merged_ci_is_both_declared_and_observed() -> None:
    """The healthy case. Collapsing this into one flag made every correctly
    declared, running service look undocumented."""
    folded = merge(
        [
            DiscoveredCI("service", "skgateway", "fleet:service"),
            DiscoveredCI("service", "skgateway", "systemd--user", observed=True),
        ]
    )
    assert folded[0].declared is True
    assert folded[0].observed is True
    assert drift(folded) == []


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


def test_partial_scan_never_turns_absence_into_an_orphan(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("skgateway", "service", tags=[DISCOVERED_TAG])
    assert reconcile(mgr, [], apply=True, scan_complete=False).orphans == []


def test_reconcile_links_existing_alias_duplicates_without_rewriting_ids(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    canonical = mgr.create_ci(
        "alpha.example", "host", attributes={"aliases": ["192.0.2.10"]},
        tags=[DISCOVERED_TAG],
    )
    duplicate = mgr.create_ci(
        "ssh-alpha", "host", attributes={"aliases": ["192.0.2.10"]},
        tags=[DISCOVERED_TAG],
    )
    sighting = DiscoveredCI(
        "host", "alpha.example", "fleet:node", canonical_name="alpha.example",
        aliases=("ssh-alpha", "192.0.2.10"),
    )
    reconcile(mgr, [sighting], apply=True)
    migrated = mgr.get_ci(duplicate.id)
    assert canonical.id != duplicate.id
    assert any(r.rel_type == "alias_of" and r.target == canonical.id for r in migrated.relationships)


def _service(name: str, active_state: str, **kw) -> DiscoveredCI:
    attrs = {"active_state": active_state}
    attrs.update(kw.pop("attributes", {}))
    return DiscoveredCI(
        "service",
        name,
        "systemd",
        observed=True,
        attributes=attrs,
        tags=(DISCOVERED_TAG,),
        **kw,
    )


def test_reconcile_derives_operational_from_active_state(tmp_path: Path) -> None:
    """CMDB-6: a discovery-owned CI that reads degraded while observed active
    must be corrected to operational on the next reconcile."""
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "skgateway")
    mgr.create_ci("skgateway", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci_id, "cmdb-seed", CIStatus.DEGRADED.value, note="from incident health")

    report = reconcile(mgr, [_service("skgateway", "active")], apply=True)

    assert "status" in report.updated[ci_id]
    assert mgr.get_ci(ci_id).status == CIStatus.OPERATIONAL.value


def test_reconcile_derives_down_from_failed_state(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "skgateway")
    mgr.create_ci("skgateway", "service", tags=[DISCOVERED_TAG])

    report = reconcile(mgr, [_service("skgateway", "failed")], apply=True)

    assert "status" in report.updated[ci_id]
    assert mgr.get_ci(ci_id).status == CIStatus.DOWN.value


def test_reconcile_status_changes_listed_as_status_not_attribute(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "skchat")
    mgr.create_ci("skchat", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci_id, "cmdb-seed", CIStatus.DEGRADED.value)

    before = [_service("skchat", "active", attributes={"active_state": "inactive"})]
    reconcile(mgr, before, apply=True)

    after = [_service("skchat", "active")]
    report = reconcile(mgr, after, apply=True)

    assert report.updated[ci_id] == ["status", "active_state"]


def test_reconcile_does_not_force_status_for_inactive_state(tmp_path: Path) -> None:
    """inactive is ambiguous (oneshots, timers); existing status is left alone."""
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "nightly")
    mgr.create_ci("nightly", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci_id, "cmdb-seed", CIStatus.OPERATIONAL.value)

    report = reconcile(mgr, [_service("nightly", "inactive")], apply=True)

    assert "status" not in [k for v in report.updated.values() for k in v]
    assert mgr.get_ci(ci_id).status == CIStatus.OPERATIONAL.value


def test_reconcile_never_un_retires_a_ci(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "gone")
    mgr.create_ci("gone", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci_id, "op", CIStatus.RETIRED.value)

    report = reconcile(mgr, [_service("gone", "active")], apply=True)

    assert "status" not in [k for v in report.updated.values() for k in v]
    assert mgr.get_ci(ci_id).status == CIStatus.RETIRED.value


def test_reconcile_does_not_force_status_for_declared_only(tmp_path: Path) -> None:
    """Declarations carry no observed health; status stays untouched."""
    mgr = CMDBManager(tmp_path)
    ci_id = make_ci_id("service", "skchat")
    mgr.create_ci("skchat", "service", tags=[DISCOVERED_TAG])
    mgr.set_status(ci_id, "cmdb-seed", CIStatus.DEGRADED.value)

    declared = DiscoveredCI("service", "skchat", "fleet:service", tags=(DISCOVERED_TAG,))
    report = reconcile(mgr, [declared], apply=True)

    assert "status" not in [k for v in report.updated.values() for k in v]
    assert mgr.get_ci(ci_id).status == CIStatus.DEGRADED.value


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


def test_drift_ignores_distro_units_but_keeps_operator_ones() -> None:
    """300 distro units must not bury the 20 findings that matter."""
    found = [
        DiscoveredCI(
            "service", "ModemManager", "systemd--system", observed=True,
            attributes={"origin": ORIGIN_DISTRO},
        ),
        DiscoveredCI(
            "service", "my-cron-hack", "systemd--user", observed=True,
            attributes={"origin": ORIGIN_OPERATOR},
        ),
    ]
    findings = drift(found)
    assert [f.ci_id for f in findings] == [make_ci_id("service", "my-cron-hack")]


def test_drift_reports_unknown_origin_rather_than_hiding_it() -> None:
    """If FragmentPath lookup fails, that must be loud, not silently clean."""
    found = [
        DiscoveredCI(
            "service", "mystery", "systemd--user", observed=True,
            attributes={"origin": ORIGIN_UNKNOWN},
        )
    ]
    assert [f.kind for f in drift(found)] == ["observed_not_declared"]


def test_drift_does_not_flag_a_declared_service_running_as_a_container() -> None:
    """capauth-keystore is runtime: docker. It is not missing."""
    found = merge(
        [
            DiscoveredCI("service", "capauth-keystore", "fleet:service"),
            DiscoveredCI("service", "capauth-keystore", "docker", observed=True),
        ]
    )
    assert drift(found) == []


def test_drift_does_not_flag_a_declared_cronjob_running_as_a_timer() -> None:
    """A fleet cronjob runs as a .timer, not a .service."""
    found = [
        DiscoveredCI("service", "capauth-custody-doctor", "fleet:cronjob"),
        DiscoveredCI(
            "service", "capauth-custody-doctor", "systemd--user", observed=True,
            attributes={"origin": ORIGIN_OPERATOR, "systemd_kind": "timer"},
        ),
    ]
    assert drift(merge(found)) == []


def test_systemd_collector_reads_timers_and_classifies_origin() -> None:
    runner = FakeRunner(
        answers={
            "--type=service": SYSTEMD_OUTPUT,
            "--type=timer": TIMER_OUTPUT,
            "show": SHOW_OUTPUT,
        }
    )
    found = collect_systemd_units(runner, scopes=("--user",))

    by_name = {c.name: c for c in found}
    assert "backup" in by_name, "timers are assets too"
    assert by_name["backup"].attributes["systemd_kind"] == "timer"
    assert by_name["skgateway"].attributes["origin"] == ORIGIN_OPERATOR
    assert by_name["skchat-daemon"].attributes["origin"] == ORIGIN_DISTRO


def test_fragment_lookup_names_units_explicitly_instead_of_globbing() -> None:
    """`systemctl show '*.service'` matched 78 of 211 real units, silently
    leaving two thirds unclassified and reported as drift."""
    runner = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "show": SHOW_OUTPUT}
    )
    collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    show_calls = [c for c in runner.calls if "show" in c]
    assert show_calls, "a fragment lookup must happen"
    assert "*.service" not in show_calls[0], "globbing is what dropped the units"
    assert "dead-thing.service" in show_calls[0], "inactive units need classifying too"


def test_not_found_units_are_not_assets() -> None:
    """A unit some dependency references but nothing installed is a dangling
    reference, not a configuration item."""
    runner = FakeRunner(answers={"--type=service": NOT_FOUND_OUTPUT})
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))
    assert [c.name for c in found] == ["skgateway"]


def test_systemd_origin_is_unknown_when_fragment_lookup_fails() -> None:
    runner = FakeRunner(answers={"--type=service": SYSTEMD_OUTPUT})
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))
    assert {c.attributes["origin"] for c in found} == {ORIGIN_UNKNOWN}


def test_docker_containers_are_observed_services() -> None:
    runner = FakeRunner(answers={"docker": DOCKER_OUTPUT})
    found = collect_docker_containers(runner)

    by_name = {c.name: c for c in found}
    assert set(by_name) == {"skchat-coturn", "skmem-pg"}
    assert by_name["skchat-coturn"].attributes["image"] == "coturn/coturn:4.6"
    assert by_name["skmem-pg"].attributes["origin"] == "container"
    assert all(c.observed for c in found)


def test_docker_collector_is_empty_without_docker() -> None:
    assert collect_docker_containers(FakeRunner(answers={})) == []


def test_drift_reports_stored_cis_no_collector_saw(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("decommissioned", "service", tags=[DISCOVERED_TAG])

    findings = drift([], mgr)

    assert [f.kind for f in findings] == ["stored_not_discovered"]
