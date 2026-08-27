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
    ci_observation_state,
    collect_agents,
    collect_cron_jobs,
    collect_datastores,
    collect_docker_containers,
    collect_fleet_objects,
    collect_host_facts,
    collect_listening_ports,
    collect_model_endpoints,
    collect_network_interfaces,
    collect_observed_agents,
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

FINDMNT_OUTPUT = json.dumps(
    {
        "filesystems": [
            {
                "target": "/",
                "source": "/dev/sda2",
                "fstype": "ext4",
                "size": 1000,
                "used": 400,
                "avail": 600,
                "use%": "40%",
                "children": [
                    {
                        "target": "/mnt/data",
                        "source": "server:/data",
                        "fstype": "nfs4",
                    },
                    {"target": "/run", "source": "tmpfs", "fstype": "tmpfs"},
                ],
            },
            {"target": "/proc", "source": "proc", "fstype": "proc"},
        ]
    }
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
    assert all(
        not c.observed for c in found
    ), "specs are declarations, never observations"


def test_operatorapp_kind_is_preserved_through_the_fold(tmp_path: Path) -> None:
    """A CLI-invoked tool (spec.cli) must carry a kind marker through the
    service/cronjob/operatorapp fold, or drift() has no way to tell it apart
    from a Service that is actually expected to have a running unit."""
    root = tmp_path / "fleet" / "objects" / "operatorapp"
    root.mkdir(parents=True)
    (root / "cmdb.json").write_text(
        json.dumps(
            {
                "kind": "Operatorapp",
                "name": "cmdb",
                "spec": {"cli": "skcapstone cmdb operator"},
            }
        )
    )

    found = collect_fleet_objects(tmp_path)

    assert len(found) == 1
    cmdb = found[0]
    assert cmdb.ci_type == CIType.SERVICE.value
    assert cmdb.attributes["fleet_kind"] == "Operatorapp"
    assert cmdb.attributes["cli"] == "skcapstone cmdb operator"


def test_registry_entries_stay_declared_with_their_timestamp(home: Path) -> None:
    found = collect_registry(home)
    assert len(found) == 1
    entry = found[0]
    assert entry.observed is False, "a registry file is a claim, not a live probe"
    assert entry.attributes["registered_at"].startswith("2026-08-17")


def test_fleet_service_spec_unit_becomes_an_alias(tmp_path: Path) -> None:
    """The smoking gun: skgateway-claude-wrapper's spec.unit names
    claude-code-api.service, that unit is active, and nothing read spec.unit
    to build the alias set drift() matches against -- dead data."""
    root = tmp_path / "fleet" / "objects" / "service"
    root.mkdir(parents=True)
    (root / "skgateway-claude-wrapper.json").write_text(
        json.dumps(
            {
                "kind": "Service",
                "name": "skgateway-claude-wrapper",
                "spec": {"runtime": "systemd", "unit": "claude-code-api.service"},
            }
        )
    )

    found = collect_fleet_objects(tmp_path)

    assert len(found) == 1
    svc = found[0]
    assert svc.aliases == ("claude-code-api.service",)
    assert "claude-code-api.service" in svc.identity_aliases


def test_registry_pid_file_stem_becomes_an_alias_when_it_differs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    root.mkdir()
    (root / "skvoice.json").write_text(
        json.dumps({"name": "skvoice", "pid_file": "/home/cbrd21/.skvoice/daemon.pid"})
    )
    (root / "skgateway.json").write_text(
        json.dumps({"name": "skgateway", "pid_file": None})
    )

    found = {c.name: c for c in collect_registry(tmp_path)}

    assert found["skvoice"].aliases == ("daemon",)
    assert found["skgateway"].aliases == (), "no pid_file, nothing to alias"


def test_drift_matches_a_declared_service_against_its_spec_unit_alias() -> None:
    declared = DiscoveredCI(
        "service",
        "skgateway-claude-wrapper",
        "fleet:service",
        aliases=("claude-code-api.service",),
    )
    observed = DiscoveredCI(
        "service", "claude-code-api", "systemd", observed=True, node="testnode"
    )
    assert drift([declared, observed]) == []


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
    assert (
        "runs_on",
        make_ci_id(CIType.HOST.value, "testnode"),
    ) in gateway.relationships

    failed = next(c for c in found if c.name == "dead-thing")
    assert (
        failed.attributes["active_state"] == "failed"
    ), "a failed unit is still an asset"


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
    runner = FakeRunner(
        answers={"ss": SS_EPHEMERAL_OUTPUT, "ip_local_port_range": "32768\t60999\n"}
    )
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
            "lscpu": json.dumps(
                {
                    "lscpu": [
                        {"field": "Socket(s):", "data": "1"},
                        {"field": "Core(s) per socket:", "data": "4"},
                        {"field": "Thread(s) per core:", "data": "2"},
                        {"field": "Model name:", "data": "Pengu CPU"},
                    ]
                }
            ),
            "free": "              total used free shared buff/cache available\nMem: 1000 400 200 10 400 500\n",
            "ip -j": json.dumps(
                [
                    {
                        "ifname": "eth0",
                        "addr_info": [{"local": "192.0.2.10", "scope": "global"}],
                    }
                ]
            ),
            "lsblk": json.dumps(
                {
                    "blockdevices": [
                        {"name": "sda", "type": "disk", "size": 10000, "children": []}
                    ]
                }
            ),
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
    assert "192.0.2.10" not in host.identity_aliases


def test_host_fact_failures_are_missing_not_zero() -> None:
    host = collect_host_facts(FakeRunner(answers={"uname": "Linux 6.8\n"}))[0]
    assert "memory_total_bytes" not in host.attributes
    assert "disk_capacity_bytes" not in host.attributes
    assert "cpu_logical" not in host.attributes


def test_cron_jobs_are_observed_without_persisting_command_arguments() -> None:
    runner = FakeRunner(
        answers={
            "/etc/crontab": "0 2 * * * root /usr/local/sbin/backup --password nope\n",
            "crontab": "*/5 * * * * /usr/local/bin/reconcile --token secret-value\n",
        }
    )
    found = collect_cron_jobs(runner)

    assert len(found) == 2
    assert {ci.attributes["command_name"] for ci in found} == {"reconcile", "backup"}
    assert all(ci.attributes["command_arguments_redacted"] for ci in found)
    assert "secret-value" not in json.dumps([ci.attributes for ci in found])
    assert "nope" not in json.dumps([ci.attributes for ci in found])


def test_cron_jobs_wrapped_by_sk_cron_run_carry_the_declared_job_name_as_an_alias() -> (
    None
):
    """Fleet-managed cron entries are dispatched as
    ``sk-cron-run.sh <job-name> <command> [args]``, prefixed by inline
    ``VAR=value`` assignments. The fingerprinted name stays the CI's stable
    identity (append-only CMDB, no renaming), but the declared job name must
    surface as an alias or a fleet cronjob spec can never match it."""
    runner = FakeRunner(
        answers={
            "/etc/crontab": "",
            "crontab": (
                "22 */3 * * * SKOS_SCHEDULE_ENV=/x/env.sh PATH=/a:/b "
                "/home/cbrd21/clawd/skos/scripts/sk-cron-run.sh ingest-order "
                "/home/cbrd21/.skenv/bin/skos ingest order --token secret-value "
                ">> /home/cbrd21/.skcapstone/logs/sk-ingest.log 2>&1\n"
            ),
        }
    )
    found = collect_cron_jobs(runner)

    assert (
        len(found) == 1
    ), "the inline VAR=value prefix must not make the whole line invisible"
    job = found[0]
    assert job.name.startswith(
        "testnode:cron:user:"
    ), "identity stays the fingerprint, not renamed"
    assert job.aliases == ("ingest-order",)
    assert "ingest-order" in job.identity_aliases
    assert job.attributes["cron_run_name"] == "ingest-order"
    assert "secret-value" not in json.dumps(job.attributes), "arguments stay redacted"


def test_cron_jobs_without_the_wrapper_get_no_cron_run_alias() -> None:
    runner = FakeRunner(answers={"crontab": "*/5 * * * * /usr/local/bin/reconcile\n"})
    found = collect_cron_jobs(runner)

    assert found[0].aliases == ()
    assert "cron_run_name" not in found[0].attributes


def test_bare_crontab_environment_lines_are_still_skipped() -> None:
    """SHELL=/bin/bash and MAILTO=... are crontab environment declarations,
    not scheduled jobs, and must not be mistaken for one."""
    runner = FakeRunner(
        answers={
            "crontab": (
                "SHELL=/bin/bash\nMAILTO=ops@example.com\n"
                "*/5 * * * * /usr/local/bin/reconcile\n"
            )
        }
    )
    found = collect_cron_jobs(runner)
    assert len(found) == 1
    assert found[0].attributes["command_name"] == "reconcile"


def test_drift_matches_a_declared_cronjob_against_its_sk_cron_run_alias() -> None:
    """The false-positive this bug produced: a fleet cronjob spec named
    'ingest-order' can never match an observed CI named
    'testnode:cron:user:<hash>' by bare name -- it must match through the
    sk-cron-run-declared alias instead."""
    declared = DiscoveredCI("service", "ingest-order", "fleet:cronjob")
    observed = DiscoveredCI(
        "service",
        "testnode:cron:user:deadbeefcafefeed",
        "cron:user",
        observed=True,
        node="testnode",
        aliases=("ingest-order",),
    )
    assert drift(merge([declared, observed])) == []
    assert (
        drift([declared, observed]) == []
    ), "must match even before folding, via alias keys"


def test_network_interfaces_are_first_class_cis() -> None:
    runner = FakeRunner(
        host="alpha",
        answers={
            "ip -j": json.dumps(
                [
                    {"ifname": "lo", "addr_info": []},
                    {
                        "ifname": "eth0",
                        "operstate": "UP",
                        "address": "02:00:00:00:00:01",
                        "addr_info": [{"local": "192.0.2.10", "scope": "global"}],
                    },
                ]
            )
        },
    )
    found = collect_network_interfaces(runner)

    assert [ci.name for ci in found] == ["alpha:eth0"]
    assert found[0].ci_type == CIType.NETWORK.value
    assert found[0].attributes["addresses"] == ["192.0.2.10"]


def test_mounts_and_database_containers_are_datastore_cis() -> None:
    runner = FakeRunner(
        host="alpha",
        answers={
            "findmnt": FINDMNT_OUTPUT,
            "docker ps": "skmem-pg\tpostgres:16\nweb\tnginx:latest\n",
        },
    )
    found = collect_datastores(runner)

    assert {ci.name for ci in found} == {
        "alpha:mount:/",
        "alpha:mount:/mnt/data",
        "alpha:container:skmem-pg",
    }
    assert all(ci.ci_type == CIType.DATASTORE.value for ci in found)
    assert not any(ci.attributes.get("mountpoint") == "/proc" for ci in found)


def test_remote_agent_homes_are_observed_on_their_host() -> None:
    found = collect_observed_agents(
        FakeRunner(host="alpha", answers={"python3": '["jarvis", "lumina-template"]\n'})
    )

    assert [ci.name for ci in found] == ["jarvis"]
    assert found[0].observed is True
    assert found[0].node == "alpha"


def test_model_endpoint_records_health_version_and_bounded_models() -> None:
    runner = FakeRunner(
        host="alpha",
        answers={
            "ollama list": "NAME ID SIZE MODIFIED\nqwen3:latest abc 1GB now\n",
            "api/version": '{"version":"0.11.0"}\n',
        },
    )
    endpoint = collect_model_endpoints(runner)[0]

    assert endpoint.name == "alpha:ollama"
    assert endpoint.attributes["health_observed"] is True
    assert endpoint.attributes["version"] == "0.11.0"
    assert endpoint.attributes["models"] == ["qwen3:latest"]


def test_shared_interface_addresses_do_not_merge_distinct_hosts() -> None:
    """Container bridges reuse RFC1918 addresses on unrelated machines."""
    answers = {
        "uname": "Linux 6.8\n",
        "ip -j": json.dumps(
            [
                {
                    "ifname": "docker0",
                    "addr_info": [{"local": "172.17.0.1", "scope": "global"}],
                }
            ]
        ),
    }

    alpha = collect_host_facts(FakeRunner(host="alpha", answers=answers))[0]
    beta = collect_host_facts(FakeRunner(host="beta", answers=answers))[0]

    assert len(merge([alpha, beta])) == 2


def test_alias_overlap_merges_host_sightings_under_declared_canonical_name() -> None:
    declared = DiscoveredCI(
        "host",
        "alpha.example",
        "fleet:node",
        canonical_name="alpha.example",
        aliases=("ssh-alpha",),
    )
    observed = DiscoveredCI(
        "host",
        "ssh-alpha",
        "host",
        observed=True,
        canonical_name="ssh-alpha",
        aliases=("ssh-alpha",),
    )
    folded = merge([declared, observed])
    assert len(folded) == 1
    assert folded[0].ci_id == make_ci_id("host", "alpha.example")
    assert set(folded[0].identity_aliases) == {"alpha.example", "ssh-alpha"}


def test_fingerprint_only_asset_is_an_unmanaged_device() -> None:
    device = device_from_fingerprint("printer", "arp", aliases=("192.0.2.50",))
    assert device.ci_type == CIType.DEVICE.value
    assert device.attributes["managed"] is False
    assert "unmanaged" in device.tags


def test_observation_freshness_is_not_health() -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    assert observation_state("") is ObservationState.UNKNOWN
    assert (
        observation_state((now - timedelta(hours=1)).isoformat(), now=now)
        is ObservationState.FRESH
    )
    assert (
        observation_state((now - timedelta(days=1)).isoformat(), now=now)
        is ObservationState.STALE
    )


def test_ci_observation_freshness_ignores_stored_claim(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    mgr = CMDBManager(tmp_path)
    ci = mgr.create_ci(
        "host",
        "host",
        attributes={
            "observed_at": (now - timedelta(days=1)).isoformat(),
            "observation_state": "fresh",
        },
    )
    assert ci_observation_state(ci, now=now) is ObservationState.STALE


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
    found = [
        DiscoveredCI("service", "skgateway", "fleet:service", tags=(DISCOVERED_TAG,))
    ]

    report = reconcile(mgr, found, apply=False)

    assert report.created == [make_ci_id("service", "skgateway")]
    assert report.applied is False
    assert mgr.list_cis() == [], "a dry run must not touch the store"


def test_reconcile_apply_creates_cis_and_relationships(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    host_id = make_ci_id(CIType.HOST.value, "alpha01")
    found = [
        DiscoveredCI(
            "host",
            "alpha01",
            "host-facts",
            observed=True,
            tags=(DISCOVERED_TAG,),
        ),
        DiscoveredCI(
            "service",
            "skgateway",
            "systemd",
            observed=True,
            node="alpha01",
            attributes={"active_state": "active"},
            tags=(DISCOVERED_TAG,),
            relationships=(("runs_on", host_id),),
        ),
    ]

    reconcile(mgr, found, apply=True)

    stored = mgr.list_cis()
    by_id = {ci.id: ci for ci in stored}
    assert set(by_id) == {host_id, make_ci_id("service", "skgateway")}
    service = by_id[make_ci_id("service", "skgateway")]
    assert service.attributes["active_state"] == "active"
    assert [(r.rel_type, r.target) for r in service.relationships] == [
        ("runs_on", host_id)
    ]


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    found = [
        DiscoveredCI(
            "service",
            "skgateway",
            "systemd",
            attributes={"port": 18991},
            tags=(DISCOVERED_TAG,),
        )
    ]

    reconcile(mgr, found, apply=True)
    second = reconcile(mgr, found, apply=True)

    assert second.created == []
    assert second.updated == {}
    assert second.unchanged == [make_ci_id("service", "skgateway")]


def test_reconcile_reports_relationships_and_drops_secret_attributes(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    host = DiscoveredCI("host", "chiap04", "fleet:node", tags=(DISCOVERED_TAG,))
    service = DiscoveredCI(
        "service",
        "api",
        "systemd",
        attributes={"password": "must-not-persist", "port": 443},
        tags=(DISCOVERED_TAG,),
        relationships=(("runs_on", host.ci_id),),
    )

    report = reconcile(mgr, [host, service], apply=True)

    assert report.relationships == [
        {
            "ci_id": service.ci_id,
            "action": "add",
            "rel_type": "runs_on",
            "target": host.ci_id,
        }
    ]
    assert report.secret_redaction_findings == [
        {"ci_id": service.ci_id, "path": "attributes.password"}
    ]
    assert "password" not in mgr.get_ci(service.ci_id).attributes


def test_reconcile_malformed_evidence_fails_before_any_write(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    malformed = DiscoveredCI(
        "service",
        "api",
        "systemd",
        relationships=(("runs_on", "ci-host-missing"),),
    )

    report = reconcile(mgr, [malformed], apply=True)

    assert report.applied is False
    assert report.validation_failures[0]["reason"] == "missing target: ci-host-missing"
    assert mgr.list_cis() == []


def test_legacy_seed_is_a_versioned_declared_discovery_bridge(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "api.json").write_text(json.dumps({"name": "api"}))

    result = CMDBManager(tmp_path).seed_from_inventory()

    assert result["schema"] == "skcoord.cmdb.compat-seed/v1"
    assert result["deprecated"] is True
    names = {ci.name for ci in CMDBManager(tmp_path).list_cis()}
    assert names == {"api"}


def test_reconcile_records_changed_attributes(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    before = [
        DiscoveredCI(
            "service",
            "skgateway",
            "systemd",
            attributes={"active_state": "active"},
            tags=(DISCOVERED_TAG,),
        )
    ]
    reconcile(mgr, before, apply=True)

    after = [
        DiscoveredCI(
            "service",
            "skgateway",
            "systemd",
            attributes={"active_state": "failed"},
            tags=(DISCOVERED_TAG,),
        )
    ]
    report = reconcile(mgr, after, apply=True)

    ci_id = make_ci_id("service", "skgateway")
    assert report.updated == {ci_id: ["active_state"]}
    assert mgr.get_ci(ci_id).attributes["active_state"] == "failed"


def test_reconcile_converges_owned_metadata_tags_relationships_and_scope(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    host_a = mgr.create_ci("host-a", "host")
    host_b = mgr.create_ci("host-b", "host")
    item = DiscoveredCI(
        "service",
        "api",
        "systemd",
        observed=True,
        node="host-a",
        tags=(DISCOVERED_TAG, "old"),
        relationships=(("runs_on", host_a.id),),
        authority="network:nor",
        lifecycle_scope="scope-one",
    )
    reconcile(mgr, [item], apply=True)

    moved = DiscoveredCI(
        "service",
        "api",
        "systemd",
        observed=True,
        node="host-b",
        description="managed API",
        tags=(DISCOVERED_TAG, "new"),
        relationships=(("runs_on", host_b.id),),
        authority="network:nor",
        lifecycle_scope="scope-two",
    )
    report = reconcile(mgr, [moved], apply=True)
    ci = mgr.get_ci(item.ci_id)
    assert ci.node == "host-b"
    assert ci.description == "managed API"
    assert set(ci.tags) == {DISCOVERED_TAG, "new"}
    assert [(rel.rel_type, rel.target, rel.authority) for rel in ci.relationships] == [
        ("runs_on", host_b.id, "network:nor")
    ]
    assert ci.attributes["lifecycle_scope"] == "scope-two"
    assert any(change.startswith("remove:runs_on") for change in report.updated[ci.id])


def test_reconcile_preserves_unowned_manual_relationship_and_tag(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    host = mgr.create_ci("host", "host")
    ci = mgr.create_ci("api", "service", tags=["manual"])
    mgr.add_relationship(ci.id, "human", "depends_on", host.id)
    reconcile(
        mgr,
        [DiscoveredCI("service", "api", "systemd", authority="network:nor")],
        apply=True,
    )
    folded = mgr.get_ci(ci.id)
    assert "manual" in folded.tags
    assert any(rel.target == host.id for rel in folded.relationships)


def test_managed_host_promotes_matching_device_without_rewriting_device_core(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    device = mgr.create_ci(
        "printer-device",
        "device",
        attributes={"aliases": ["192.0.2.50"]},
        tags=[DISCOVERED_TAG],
    )
    host = DiscoveredCI(
        "host",
        "printer.local",
        "fleet:node",
        aliases=("192.0.2.50",),
        authority="declared",
    )
    reconcile(mgr, [host], apply=True)
    promoted = mgr.get_ci(host.ci_id)
    retained = mgr.get_ci(device.id)
    assert promoted.ci_type == "host"
    assert retained.ci_type == "device"
    assert any(
        rel.rel_type == "alias_of" and rel.target == promoted.id
        for rel in retained.relationships
    )


def test_drift_uses_same_alias_identity_resolution_as_reconcile(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    stored = mgr.create_ci(
        "alpha.example",
        "host",
        attributes={"aliases": ["192.0.2.10"]},
        tags=[DISCOVERED_TAG],
    )
    sighting = DiscoveredCI(
        "host", "ssh-alpha", "host", observed=True, aliases=("192.0.2.10",)
    )
    findings = drift([sighting], mgr)
    assert not any(
        finding.kind == "stored_not_discovered" and finding.ci_id == stored.id
        for finding in findings
    )


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


def test_reconcile_links_existing_alias_duplicates_without_rewriting_ids(
    tmp_path: Path,
) -> None:
    mgr = CMDBManager(tmp_path)
    canonical = mgr.create_ci(
        "alpha.example",
        "host",
        attributes={"aliases": ["192.0.2.10"]},
        tags=[DISCOVERED_TAG],
    )
    duplicate = mgr.create_ci(
        "ssh-alpha",
        "host",
        attributes={"aliases": ["192.0.2.10"]},
        tags=[DISCOVERED_TAG],
    )
    sighting = DiscoveredCI(
        "host",
        "alpha.example",
        "fleet:node",
        canonical_name="alpha.example",
        aliases=("ssh-alpha", "192.0.2.10"),
    )
    reconcile(mgr, [sighting], apply=True)
    migrated = mgr.get_ci(duplicate.id)
    assert canonical.id != duplicate.id
    assert any(
        r.rel_type == "alias_of" and r.target == canonical.id
        for r in migrated.relationships
    )


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
    mgr.set_status(
        ci_id, "cmdb-seed", CIStatus.DEGRADED.value, note="from incident health"
    )

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


def test_reconcile_status_changes_listed_as_status_not_attribute(
    tmp_path: Path,
) -> None:
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

    declared = DiscoveredCI(
        "service", "skchat", "fleet:service", tags=(DISCOVERED_TAG,)
    )
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


def test_drift_does_not_demand_a_running_unit_for_an_operatorapp() -> None:
    """cmdb is CLI-invoked by design (reconcile-timer + CLI, not a daemon).
    No running unit is the correct steady state, not a gap."""
    declared = DiscoveredCI(
        "service",
        "cmdb",
        "fleet:operatorapp",
        attributes={"fleet_kind": "Operatorapp", "cli": "skcapstone cmdb operator"},
    )
    assert drift([declared]) == []


def test_drift_operatorapp_exemption_does_not_swallow_a_real_service_gap() -> None:
    """The Operatorapp exemption must be narrow: a plain Service with no
    fleet_kind marker still needs a running unit."""
    declared = DiscoveredCI("service", "skgateway", "fleet:service")
    assert [f.kind for f in drift([declared])] == ["declared_not_observed"]


def test_drift_flags_a_service_running_that_nothing_declared() -> None:
    found = [
        DiscoveredCI(
            "service", "mystery-thing", "systemd", observed=True, node="alpha01"
        )
    ]
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
            "service",
            "ModemManager",
            "systemd--system",
            observed=True,
            attributes={"origin": ORIGIN_DISTRO},
        ),
        DiscoveredCI(
            "service",
            "my-cron-hack",
            "systemd--user",
            observed=True,
            attributes={"origin": ORIGIN_OPERATOR},
        ),
    ]
    findings = drift(found)
    assert [f.ci_id for f in findings] == [make_ci_id("service", "my-cron-hack")]


def test_drift_reports_unknown_origin_rather_than_hiding_it() -> None:
    """If FragmentPath lookup fails, that must be loud, not silently clean."""
    found = [
        DiscoveredCI(
            "service",
            "mystery",
            "systemd--user",
            observed=True,
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
            "service",
            "capauth-custody-doctor",
            "systemd--user",
            observed=True,
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
    runner = FakeRunner(answers={"--type=service": SYSTEMD_OUTPUT, "show": SHOW_OUTPUT})
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


def test_docker_restart_identity_excludes_pid_and_run_token() -> None:
    first = FakeRunner(
        answers={
            "docker": "sklegal-s102-3620007-4e13b305\tpostgres:17.7-alpine\tUp 1 second\t\t\n"
        }
    )
    restarted = FakeRunner(
        answers={
            "docker": "sklegal-s102-3521761-73688fd1\tpostgres:17.7-alpine\tUp 1 second\t\t\n"
        }
    )

    before = collect_docker_containers(first)[0]
    after = collect_docker_containers(restarted)[0]

    assert before.ci_id == after.ci_id == "ci-service-sklegal-s102"
    assert before.attributes["launcher_pid"] == "3620007"
    assert after.attributes["restart_token"] == "73688fd1"
    assert before.aliases == ("sklegal-s102-3620007-4e13b305",)


def test_container_names_without_known_restart_suffix_are_preserved() -> None:
    runner = FakeRunner(
        answers={"docker": "api-1234-deadbeef-extra\tapi:1\tUp 1 day\t\t\n"}
    )

    found = collect_docker_containers(runner)[0]

    assert found.name == "api-1234-deadbeef-extra"
    assert "launcher_pid" not in found.attributes
    assert found.aliases == ()


def test_docker_collector_is_empty_without_docker() -> None:
    assert collect_docker_containers(FakeRunner(answers={})) == []


def test_container_runtime_inventory_is_a_fixed_non_secret_fallback() -> None:
    runner = FakeRunner(answers={"python3": '{"docker": false, "podman": false}\n'})

    assert collect_docker_containers(runner) == []
    assert runner.calls[0][:2] == ["python3", "-c"]
    assert "docker" in runner.calls[0][2]
    assert "podman" in runner.calls[0][2]


def test_drift_reports_stored_cis_no_collector_saw(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci("decommissioned", "service", tags=[DISCOVERED_TAG])

    findings = drift([], mgr)

    assert [f.kind for f in findings] == ["stored_not_discovered"]
