"""Observed systemd and container discovery collectors."""

from __future__ import annotations

import logging
import re
from typing import Any, Sequence

from .cmdb import CIType, make_ci_id
from .discovery_base import DISCOVERED_TAG, CommandRunner, DiscoveredCI

logger = logging.getLogger("skcoord.discovery")

_UNIT_RE = re.compile(
    r"^(?P<unit>[\w@:.\\-]+)\.(?P<kind>service|timer)\s+(?P<load>\S+)\s+(?P<active>\S+)\s+(?P<sub>\S+)"
)

ORIGIN_OPERATOR = "operator"
ORIGIN_DISTRO = "distro"
ORIGIN_UNKNOWN = "unknown"

_DISTRO_UNIT_DIRS = ("/usr/lib/systemd/", "/lib/systemd/", "/usr/local/lib/systemd/")
_CONTAINER_RESTART_SUFFIX = re.compile(
    r"^(?P<stable>.+)-(?P<pid>[1-9]\d*)-(?P<token>[0-9a-f]{8})$"
)


def _stable_container_name(name: str) -> tuple[str, dict[str, str]]:
    """Remove the known per-restart PID and token suffix from a container name.

    The SKLegal job launcher names PostgreSQL containers
    ``<subject>-<pid>-<8 hex run token>``. Those process-local fields are useful
    evidence, but they are not configuration identity. Keep them as attributes
    and retain the raw name as an alias while returning the stable subject used
    for the CI id and drift deduplication.
    """
    match = _CONTAINER_RESTART_SUFFIX.fullmatch(name)
    if not match:
        return name, {}
    return match.group("stable"), {
        "observed_container_name": name,
        "launcher_pid": match.group("pid"),
        "restart_token": match.group("token"),
    }


def _classify_origin(fragment_path: str) -> str:
    """Who authored this unit: the distro, or us.

    A distro unit is not an asset anyone forgot to declare, so drift must not
    report it. An operator-authored unit under /etc or ~/.config with no fleet
    object behind it is exactly the thing worth knowing about.
    """
    if not fragment_path:
        return ORIGIN_UNKNOWN
    if any(d in fragment_path for d in _DISTRO_UNIT_DIRS):
        return ORIGIN_DISTRO
    return ORIGIN_OPERATOR


_SHOW_CHUNK = 400


def _fragment_paths(
    runner: CommandRunner, scope: str, units: Sequence[str]
) -> dict[str, str]:
    """Bulk Id -> FragmentPath for named units, in as few systemctl calls as possible.

    The units are named explicitly rather than globbed. ``systemctl show
    '*.service'`` only matched 78 of 211 loaded units on a real node, which
    silently left two thirds of them unclassified and therefore reported as
    drift. Chunked so a host with thousands of units cannot blow ARG_MAX.
    """
    paths: dict[str, str] = {}
    for start in range(0, len(units), _SHOW_CHUNK):
        chunk = list(units[start : start + _SHOW_CHUNK])
        if not chunk:
            continue
        stdout = runner.run(
            [
                "systemctl",
                scope,
                "show",
                *chunk,
                "-p",
                "Id",
                "-p",
                "FragmentPath",
                "--no-pager",
            ]
        )
        if not stdout:
            continue
        unit_id = ""
        for line in stdout.splitlines():
            if line.startswith("Id="):
                unit_id = line[3:].strip()
            elif line.startswith("FragmentPath=") and unit_id:
                paths[unit_id] = line[len("FragmentPath=") :].strip()
                unit_id = ""
    return paths


_DEP_PROPS = ("Requires", "Wants")


def _unit_dependencies(
    runner: CommandRunner, scope: str, units: Sequence[str]
) -> dict[str, set[str]]:
    """Bulk Id -> dependency unit names, chunked like :func:`_fragment_paths`.

    Hard (``Requires``) and soft (``Wants``) dependencies both become
    ``depends_on`` edges; ordering properties (``After``/``Before``) are not
    dependencies and are ignored. A failed lookup yields no edges, never a
    scan failure: missing dependency data must not cost us the units.
    """
    deps: dict[str, set[str]] = {}
    for start in range(0, len(units), _SHOW_CHUNK):
        chunk = list(units[start : start + _SHOW_CHUNK])
        if not chunk:
            continue
        argv = ["systemctl", scope, "show", *chunk, "-p", "Id"]
        for prop in _DEP_PROPS:
            argv += ["-p", prop]
        argv.append("--no-pager")
        stdout = runner.run(argv)
        if not stdout:
            continue
        unit_id = ""
        for line in stdout.splitlines():
            if line.startswith("Id="):
                unit_id = line[3:].strip()
            elif unit_id:
                prop, _, value = line.partition("=")
                if prop in _DEP_PROPS and value.split():
                    deps.setdefault(unit_id, set()).update(value.split())
    return deps


def collect_systemd_units(
    runner: CommandRunner,
    scopes: Sequence[str] = ("--user", "--system"),
    kinds: Sequence[str] = ("service", "timer"),
) -> list[DiscoveredCI]:
    """Service CIs for units actually loaded on ``runner``'s host.

    Both scopes are collected because the sk* fleet runs services under
    ``systemctl --user`` while the platform pieces they depend on are system
    units. Querying only one scope is how a node looks half-empty.

    Timers matter as much as services: a fleet cronjob runs as a ``.timer``, so
    a collector that only reads ``.service`` units reports every scheduled job
    as missing.

    Each unit also carries ``depends_on`` edges to the other loaded units it
    ``Requires``/``Wants`` in the same scope, so the CMDB dependency graph
    reflects what systemd would actually cascade a failure through. Edges to
    units that are not loaded here (targets, dangling references) are dropped:
    a relationship to a CI that does not exist is noise, not topology.
    """
    out: list[DiscoveredCI] = []
    for scope in scopes:
        rows: list[tuple[str, str, str, str, str]] = []
        for kind in kinds:
            stdout = runner.run(
                [
                    "systemctl",
                    scope,
                    "list-units",
                    f"--type={kind}",
                    "--all",
                    "--no-legend",
                    "--no-pager",
                    "--plain",
                ]
            )
            if stdout is None:
                logger.debug(
                    "systemd %s/%s unavailable on %s", scope, kind, runner.host
                )
                continue
            for line in stdout.splitlines():
                match = _UNIT_RE.match(line.strip())
                if not match or match.group("kind") != kind:
                    continue
                if match.group("load") == "not-found":
                    # Referenced by some dependency but not installed here.
                    # A dangling reference is not an asset, and listing it as
                    # one puts ~90 phantom services in the drift report.
                    continue
                rows.append(
                    (
                        match.group("unit"),
                        kind,
                        match.group("load"),
                        match.group("active"),
                        match.group("sub"),
                    )
                )

        unit_ids = [f"{unit}.{kind}" for unit, kind, *_ in rows]
        paths = _fragment_paths(runner, scope, unit_ids)
        deps = _unit_dependencies(runner, scope, unit_ids)
        observed_ids = set(unit_ids)

        for unit, kind, load, active, sub in rows:
            unit_id = f"{unit}.{kind}"
            fragment = paths.get(unit_id, "")
            self_ci_id = make_ci_id(CIType.SERVICE.value, unit)
            relationships = [("runs_on", make_ci_id(CIType.HOST.value, runner.host))]
            for dep in sorted(deps.get(unit_id, set()) - {unit_id}):
                if dep not in observed_ids:
                    continue
                dep_ci_id = make_ci_id(CIType.SERVICE.value, dep.rsplit(".", 1)[0])
                # A unit and its same-named sibling of another kind fold to ONE
                # CI here (``foo.timer`` and ``foo.service`` are both
                # ``ci-service-foo``), so systemd's ordinary timer->service
                # dependency would emit a depends_on pointing at this very CI.
                # Subtracting ``unit_id`` above only drops the identical unit,
                # not the sibling. A self edge fails validation, and
                # ``reconcile --apply`` refuses to run while any validation
                # failure is present -- so this silently blocked every apply.
                if dep_ci_id == self_ci_id:
                    continue
                relationships.append(("depends_on", dep_ci_id))
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=unit,
                    source=f"systemd{scope}",
                    observed=True,
                    node=runner.host,
                    attributes={
                        "systemd_scope": scope.lstrip("-"),
                        "systemd_kind": kind,
                        "load_state": load,
                        "active_state": active,
                        "sub_state": sub,
                        "fragment_path": fragment,
                        "origin": _classify_origin(fragment),
                    },
                    tags=("systemd", kind, DISCOVERED_TAG),
                    relationships=tuple(relationships),
                )
            )
    return out


def collect_docker_containers(runner: CommandRunner) -> list[DiscoveredCI]:
    """Service CIs for running Docker and Podman containers.

    Several fleet services declare ``runtime: docker``. Without this collector
    they are declared, never observed, and drift reports them as missing when
    they are running perfectly well.
    """
    # A target with no container runtime is a valid workstation profile, not a
    # transport failure.  This fixed, non-secret fallback makes that absence an
    # explicit partial coverage result instead of an unavailable collector.
    runner.run(
        [
            "python3",
            "-c",
            "import json,shutil; print(json.dumps({n: bool(shutil.which(n)) "
            "for n in ('docker','podman')}))",
        ]
    )
    out: list[DiscoveredCI] = []
    seen: set[str] = set()
    for runtime in ("docker", "podman"):
        stdout = runner.run(
            [
                runtime,
                "ps",
                "--format",
                "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.Labels}}",
            ]
        )
        if not stdout:
            continue
        for line in stdout.splitlines():
            parts = line.split("\t")
            if not parts or not parts[0].strip():
                continue
            observed_name = parts[0].strip()
            name, volatile_identity = _stable_container_name(observed_name)
            if name in seen:
                continue
            seen.add(name)
            attributes: dict[str, Any] = {
                "runtime": runtime,
                "origin": "container",
                **volatile_identity,
            }
            if len(parts) > 1 and parts[1].strip():
                attributes["image"] = parts[1].strip()
            if len(parts) > 2 and parts[2].strip():
                attributes["container_status"] = parts[2].strip()
            if len(parts) > 3 and parts[3].strip():
                attributes["container_ports"] = parts[3].strip()
            if len(parts) > 4 and parts[4].strip():
                labels = dict(
                    part.split("=", 1)
                    for part in parts[4].split(",")
                    if "=" in part
                    and part.split("=", 1)[0]
                    in {
                        "com.docker.compose.project",
                        "com.docker.compose.service",
                        "io.podman.compose.project",
                    }
                )
                if labels:
                    attributes["compose"] = labels
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=name,
                    source=runtime,
                    observed=True,
                    node=runner.host,
                    attributes=attributes,
                    tags=(runtime, DISCOVERED_TAG),
                    aliases=(observed_name,) if observed_name != name else (),
                    relationships=(
                        ("runs_on", make_ci_id(CIType.HOST.value, runner.host)),
                    ),
                )
            )
    return out


# ss -tlnpH columns: State Recv-Q Send-Q Local:Port Peer:Port [Process]
_SS_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<extra>.*))?$")
_PROC_RE = re.compile(r'users:\(\("(?P<proc>[^"]+)"')


_FALLBACK_EPHEMERAL = (32768, 60999)
