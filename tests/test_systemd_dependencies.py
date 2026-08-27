"""Tests for systemd-derived ``depends_on`` discovery edges.

The CMDB supports depends_on/hosts/connects_to from day one, but until the
systemd collector read Requires/Wants nothing ever wrote an edge impact
analysis could walk. These tests pin the mapping: hard and soft deps between
loaded units become edges; deps to anything not observed (targets, dangling
references, self) do not; a failed lookup costs the edges, never the units.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from skcoord.cmdb import CIType, CMDBManager, make_ci_id
from skcoord.discovery import (
    AUTHORITY_OBSERVED,
    DiscoveredCI,
    collect_systemd_units,
    reconcile,
)

SYSTEMD_OUTPUT = """skgateway.service           loaded active   running SKGateway router
skchat-daemon.service       loaded active   running SKChat daemon
skmemory.service            loaded active   running SKMemory service
"""

DEPS_OUTPUT = """Id=skgateway.service
Requires=skchat-daemon.service
Wants=skmemory.service network.target

Id=skchat-daemon.service
Requires=skmemory.service
Wants=

Id=skmemory.service
Requires=
Wants=skmemory.service
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


def _depends_on(ci) -> set[str]:
    return {target for rel_type, target in ci.relationships if rel_type == "depends_on"}


def _collect(
    runner: FakeRunner, scopes=("--user",), kinds=("service",)
) -> list[DiscoveredCI]:
    """Collect with the authority scan() would stamp, so reconcile converges edges."""
    host = DiscoveredCI(
        ci_type=CIType.HOST.value,
        name=runner.host,
        source="test",
        observed=True,
        node=runner.host,
        authority=AUTHORITY_OBSERVED,
    )
    return [host] + [
        replace(ci, authority=AUTHORITY_OBSERVED)
        for ci in collect_systemd_units(runner, scopes=scopes, kinds=kinds)
    ]


# ── happy path ────────────────────────────────────────────────────────────


def test_requires_and_wants_become_depends_on_edges() -> None:
    runner = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "Requires": DEPS_OUTPUT}
    )
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    by_name = {c.name: c for c in found}
    assert _depends_on(by_name["skgateway"]) == {
        make_ci_id(CIType.SERVICE.value, "skchat-daemon"),
        make_ci_id(CIType.SERVICE.value, "skmemory"),
    }
    assert _depends_on(by_name["skchat-daemon"]) == {
        make_ci_id(CIType.SERVICE.value, "skmemory")
    }
    assert all(
        ("runs_on", make_ci_id(CIType.HOST.value, "testnode")) in c.relationships
        for c in found
    ), "dependency edges must not cost the runs_on edge"


def test_dependency_edges_survive_reconcile_and_are_idempotent(tmp_path: Path) -> None:
    runner = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "Requires": DEPS_OUTPUT}
    )
    found = _collect(runner)
    mgr = CMDBManager(tmp_path)

    first = reconcile(mgr, found, apply=True)
    gateway_id = make_ci_id(CIType.SERVICE.value, "skgateway")
    assert gateway_id in first.created
    stored = mgr.get_ci(gateway_id)
    assert {r.target for r in stored.relationships if r.rel_type == "depends_on"} == {
        make_ci_id(CIType.SERVICE.value, "skchat-daemon"),
        make_ci_id(CIType.SERVICE.value, "skmemory"),
    }

    second = reconcile(mgr, _collect(runner), apply=True)
    assert (
        gateway_id in second.unchanged
    ), "a rescan of the same deps must not rewrite edges"


def test_removed_dependency_is_converged_on_rescan(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    with_deps = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "Requires": DEPS_OUTPUT}
    )
    reconcile(mgr, _collect(with_deps), apply=True)

    without = DEPS_OUTPUT.replace("Requires=skchat-daemon.service", "Requires=")
    runner = FakeRunner(answers={"--type=service": SYSTEMD_OUTPUT, "Requires": without})
    report = reconcile(mgr, _collect(runner), apply=True)

    gateway_id = make_ci_id(CIType.SERVICE.value, "skgateway")
    stored = mgr.get_ci(gateway_id)
    assert {r.target for r in stored.relationships if r.rel_type == "depends_on"} == {
        make_ci_id(CIType.SERVICE.value, "skmemory")
    }
    assert any("remove:depends_on" in change for change in report.updated[gateway_id])


# ── edge cases ────────────────────────────────────────────────────────────


def test_edges_to_unobserved_units_are_dropped() -> None:
    """network.target is a real dep but not a CI; self-deps are meaningless."""
    runner = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "Requires": DEPS_OUTPUT}
    )
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    by_name = {c.name: c for c in found}
    all_targets = {t for c in found for t in _depends_on(c)}
    assert make_ci_id(CIType.SERVICE.value, "network.target") not in all_targets
    assert make_ci_id(CIType.SERVICE.value, "network") not in all_targets
    assert _depends_on(by_name["skmemory"]) == set(), "a self-dependency is not an edge"


def test_edges_never_cross_scopes() -> None:
    """A user unit's dep on a system-only unit resolves to nothing."""
    system_units = "postgresql.service        loaded active   running PostgreSQL\n"
    deps = "Id=skgateway.service\nRequires=postgresql.service\n"
    runner = FakeRunner(
        answers={
            "--user": SYSTEMD_OUTPUT,
            "--system": system_units,
            "Requires": deps,
        }
    )
    found = collect_systemd_units(runner, kinds=("service",))

    gateway = next(
        c
        for c in found
        if c.name == "skgateway" and c.attributes["systemd_scope"] == "user"
    )
    assert _depends_on(gateway) == set(), "the user scope never saw postgresql"


# ── failure modes ─────────────────────────────────────────────────────────


def test_failed_dependency_lookup_keeps_the_units() -> None:
    runner = FakeRunner(answers={"--type=service": SYSTEMD_OUTPUT})
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    assert {c.name for c in found} == {"skgateway", "skchat-daemon", "skmemory"}
    assert all(_depends_on(c) == set() for c in found)
    assert all(
        ("runs_on", make_ci_id(CIType.HOST.value, "testnode")) in c.relationships
        for c in found
    )


def test_malformed_dependency_output_yields_no_edges() -> None:
    garbage = "this is not systemctl output\n\x00binary\xff\n= orphaned\n"
    runner = FakeRunner(answers={"--type=service": SYSTEMD_OUTPUT, "Requires": garbage})
    found = collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    assert len(found) == 3
    assert all(_depends_on(c) == set() for c in found)


def test_dependency_lookup_names_units_explicitly_instead_of_globbing() -> None:
    runner = FakeRunner(
        answers={"--type=service": SYSTEMD_OUTPUT, "Requires": DEPS_OUTPUT}
    )
    collect_systemd_units(runner, scopes=("--user",), kinds=("service",))

    dep_calls = [c for c in runner.calls if "Requires" in c]
    assert dep_calls, "a dependency lookup must happen"
    assert "*.service" not in dep_calls[0]
    assert "skgateway.service" in dep_calls[0]


# ── timer/service sibling folds to one CI (card 0bc46220) ─────────────────

TIMER_UNITS_OUTPUT = """nextcloud-cbrd21-sync.timer  loaded active waiting Nextcloud sync
"""
SERVICE_UNITS_OUTPUT = """nextcloud-cbrd21-sync.service  loaded active running Nextcloud sync
"""
TIMER_DEPS_OUTPUT = """Id=nextcloud-cbrd21-sync.timer
Requires=nextcloud-cbrd21-sync.service
Wants=

Id=nextcloud-cbrd21-sync.service
Requires=
Wants=
"""


def test_timer_does_not_depend_on_its_own_same_named_service() -> None:
    """A ``.timer`` and its ``.service`` fold to ONE ci-service-<base>.

    systemd's ordinary timer->service dependency therefore pointed the CI at
    itself. A self edge fails validation, and ``reconcile --apply`` refuses to
    run while any validation failure is present, so this silently blocked
    every apply on a node with a timer whose service shares its name.
    """
    runner = FakeRunner(
        answers={
            "--type=timer": TIMER_UNITS_OUTPUT,
            "--type=service": SERVICE_UNITS_OUTPUT,
            "Requires": TIMER_DEPS_OUTPUT,
        }
    )
    found = collect_systemd_units(
        runner, scopes=("--user",), kinds=("timer", "service")
    )

    self_id = make_ci_id(CIType.SERVICE.value, "nextcloud-cbrd21-sync")
    for ci in found:
        assert self_id not in _depends_on(ci), f"{ci.name} emitted a self edge"


def test_timer_service_pair_passes_validation(tmp_path: Path) -> None:
    """The end-to-end symptom: reconcile reports zero validation failures."""
    runner = FakeRunner(
        answers={
            "--type=timer": TIMER_UNITS_OUTPUT,
            "--type=service": SERVICE_UNITS_OUTPUT,
            "Requires": TIMER_DEPS_OUTPUT,
        }
    )
    found = _collect(runner, scopes=("--user",), kinds=("timer", "service"))
    report = reconcile(CMDBManager(tmp_path), found, apply=False)

    assert report.validation_failures == [], report.validation_failures
