"""Per-node fleet jobs: which node a declaration targets, and drift honouring it.

The live case (2026-11-06): a fleet enrollment declared
``skcounter-edge-41``, ``skcounter-edge-noroc2027`` and
``skcounter-edge-ollama``, one CronJob per node, each running
``systemctl --user start skcounter-edge.service`` from that node's own
``skcounter-edge.timer``. A local scan of noroc2027 reported all three as
``declared_not_observed``:

* the noroc2027 one because nothing turned ``spec.command`` into an alias, so
  the declared name could never meet ``skcounter-edge.timer``;
* the other two because drift() had no idea they target other nodes, so a
  scan that never looked at those nodes counted them as missing here.

Any newly declared per-node job flipped SKBrain's CmdbDriftBounded gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcoord.discovery import DiscoveredCI, collect_cron_jobs, collect_fleet_objects, drift, merge


class FakeRunner:
    def __init__(self, host: str = "testnode", answers: dict | None = None) -> None:
        self.host = host
        self.answers = answers or {}

    def run(self, argv):
        for key, value in self.answers.items():
            if key in " ".join(argv):
                return value
        return None


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _fleet(tmp_path: Path) -> Path:
    objects = tmp_path / "fleet" / "objects"
    _write(
        objects / "node" / "node-noroc2027.json",
        {
            "kind": "Node",
            "name": "node-noroc2027",
            "labels": {"control-plane": "true", "always-on": "true"},
            "spec": {"address": {"hostname": "noroc2027"}},
        },
    )
    _write(
        objects / "node" / "node-41.json",
        {
            "kind": "Node",
            "name": "node-41",
            "labels": {"heavy-build": "true"},
            "spec": {"address": {"hostname": "192.168.0.41"}},
        },
    )
    for node in ("noroc2027", "41"):
        _write(
            objects / "cronjob" / f"skcounter-edge-{node}.json",
            {
                "kind": "Cronjob",
                "name": f"skcounter-edge-{node}",
                "labels": {"skcounter.node": node},
                "spec": {
                    "command": "systemctl --user start skcounter-edge.service",
                    "schedule": "15m",
                    "nodeSelector": {"skcounter.node": node},
                },
            },
        )
    return tmp_path


def _by_name(found):
    return {c.name: c for c in found}


# ── collector: spec.command names the unit ────────────────────────────────


def test_systemctl_start_command_names_the_unit_as_an_alias(tmp_path: Path) -> None:
    found = _by_name(collect_fleet_objects(_fleet(tmp_path)))
    job = found["skcounter-edge-noroc2027"]
    assert "skcounter-edge.service" in job.aliases


def test_a_non_systemctl_command_adds_no_alias(tmp_path: Path) -> None:
    """A CLI command is not a unit name; guessing one from it would invent
    matches (``skos`` would meet every ``skos-*`` unit)."""
    _write(
        tmp_path / "fleet" / "objects" / "cronjob" / "skos-morning-brief.json",
        {"kind": "Cronjob", "name": "skos-morning-brief", "spec": {"command": "skos brief send"}},
    )
    (job,) = collect_fleet_objects(tmp_path)
    assert job.aliases == ()


# ── collector: which node a job targets ───────────────────────────────────


def test_fleet_status_names_the_node_a_cronjob_runs_on(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "status" / "noroc2027" / "cronjob" / "skcounter-edge-noroc2027.json",
        {"kind": "Cronjob", "name": "skcounter-edge-noroc2027", "node": "noroc2027"},
    )
    found = _by_name(collect_fleet_objects(home))
    assert found["skcounter-edge-noroc2027"].attributes["target_nodes"] == ["node-noroc2027"]


def test_a_placement_record_wins_over_status(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "placements" / "cronjob" / "skcounter-edge-41.json",
        {"kind": "Cronjob", "name": "skcounter-edge-41", "node": "node-41"},
    )
    _write(
        home / "fleet" / "status" / "noroc2027" / "cronjob" / "skcounter-edge-41.json",
        {"kind": "Cronjob", "name": "skcounter-edge-41", "node": "noroc2027"},
    )
    found = _by_name(collect_fleet_objects(home))
    assert found["skcounter-edge-41"].attributes["target_nodes"] == ["node-41"]


def test_node_selector_resolves_against_node_labels(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "objects" / "cronjob" / "skmem-health.json",
        {
            "kind": "Cronjob",
            "name": "skmem-health",
            "spec": {"command": "deploy/ops/skmem-health.sh", "nodeSelector": {"always-on": "true"}},
        },
    )
    found = _by_name(collect_fleet_objects(home))
    assert found["skmem-health"].attributes["target_nodes"] == ["node-noroc2027"]
    # skcounter.node is not a node label anywhere, and no status or placement
    # exists yet, so the scheduler could place these on no node at all.
    assert found["skcounter-edge-41"].attributes["target_nodes"] == []


def test_a_job_with_no_targeting_evidence_carries_no_target_nodes(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "objects" / "cronjob" / "anywhere.json",
        {"kind": "Cronjob", "name": "anywhere", "spec": {"command": "x.sh"}},
    )
    found = _by_name(collect_fleet_objects(home))
    assert "target_nodes" not in found["anywhere"].attributes


# ── drift: per-node jobs in a scan of one node ────────────────────────────


def _scan_of_noroc2027(home: Path, *extra: DiscoveredCI) -> list[DiscoveredCI]:
    """What a ``--local`` scan on noroc2027 hands drift()."""
    observed = [
        DiscoveredCI("host", "noroc2027", "host", observed=True, node="noroc2027"),
        DiscoveredCI(
            "service",
            "noroc2027:user:skcounter-edge.timer",
            "systemd--user",
            observed=True,
            node="noroc2027",
            aliases=("skcounter-edge.timer",),
        ),
        *extra,
    ]
    return [*collect_fleet_objects(home), *observed]


def _missing(found) -> set[str]:
    return {f.ci_id for f in drift(found) if f.kind == "declared_not_observed"}


def test_the_live_skcounter_case_reports_nothing_missing(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    for node in ("noroc2027", "41"):
        _write(
            home / "fleet" / "status" / node / "cronjob" / f"skcounter-edge-{node}.json",
            {"name": f"skcounter-edge-{node}", "node": node},
        )
    found = _scan_of_noroc2027(home)
    assert _missing(found) == set()
    assert _missing(merge(found)) == set(), "must hold after folding too"


def test_a_newly_declared_job_for_an_unscanned_node_does_not_flip_the_gate(tmp_path: Path) -> None:
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "objects" / "cronjob" / "skcounter-edge-newnode.json",
        {
            "kind": "Cronjob",
            "name": "skcounter-edge-newnode",
            "spec": {
                "command": "systemctl --user start skcounter-edge.service",
                "nodeSelector": {"skcounter.node": "newnode"},
            },
        },
    )
    assert _missing(_scan_of_noroc2027(home)) == set()


def test_a_job_targeting_the_scanned_node_with_nothing_running_is_still_missing(
    tmp_path: Path,
) -> None:
    """The gate must still bite: node targeting only removes jobs this scan
    could not have seen, never a gap on the node it did look at."""
    home = _fleet(tmp_path)
    _write(
        home / "fleet" / "objects" / "cronjob" / "skmem-health.json",
        {
            "kind": "Cronjob",
            "name": "skmem-health",
            "spec": {"command": "deploy/ops/skmem-health.sh", "nodeSelector": {"always-on": "true"}},
        },
    )
    assert _missing(_scan_of_noroc2027(home)) == {"ci-service-skmem-health"}


def test_a_per_node_job_does_not_match_a_unit_running_on_another_node(tmp_path: Path) -> None:
    """Both nodes scanned (network scope), but only noroc2027 runs the timer:
    the .41 job is missing, and noroc2027's timer must not cover for it."""
    home = _fleet(tmp_path)
    for node in ("noroc2027", "41"):
        _write(
            home / "fleet" / "status" / node / "cronjob" / f"skcounter-edge-{node}.json",
            {"name": f"skcounter-edge-{node}", "node": node},
        )
    laptop = DiscoveredCI(
        "host", "laptop", "host", observed=True, node="laptop", aliases=("192.168.0.41",)
    )
    assert _missing(_scan_of_noroc2027(home, laptop)) == {"ci-service-skcounter-edge-41"}


def test_untargeted_declarations_keep_the_old_behaviour() -> None:
    """No targeting evidence: drift still counts it wherever the scan ran."""
    found = [
        DiscoveredCI("host", "noroc2027", "host", observed=True, node="noroc2027"),
        DiscoveredCI("service", "skingest", "fleet:service"),
    ]
    assert _missing(found) == {"ci-service-skingest"}


# ── crontab: the wrapper as it is actually installed ──────────────────────


def test_sk_cron_run_without_the_sh_suffix_carries_the_job_name() -> None:
    """Every live crontab line calls ``~/.skenv/bin/sk-cron-run`` (the
    installed console script, no ``.sh``), so the job-name alias was never
    captured and no fleet cronjob could match a cron entry."""
    runner = FakeRunner(
        answers={
            "/etc/crontab": "",
            "crontab": (
                "15 3 * * * /home/cbrd21/.skenv/bin/sk-cron-run skmem-pg-backup "
                "env RETAIN=1 /bin/bash /x/skmem-pg-backup.sh >> /x/log 2>&1\n"
            )
        }
    )
    (job,) = collect_cron_jobs(runner)
    assert job.aliases == ("skmem-pg-backup",)
    assert job.attributes["cron_run_name"] == "skmem-pg-backup"
