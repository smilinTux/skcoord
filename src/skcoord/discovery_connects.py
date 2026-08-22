"""``connects_to`` edge derivation for CMDB discovery (card 6e010c63, epic 122ebff1 AC3).

The base pipeline already emits ``runs_on`` (every observed collector) and
``depends_on`` (systemd Requires/Wants). This module adds the third edge
kind, from read-only inspection only:

1. Port ownership (:func:`collect_port_service_links`). Every listener in
   ``ss -tlnpH`` names its owning process and pid. The pid's
   ``/proc/<pid>/cgroup`` names its systemd unit (``*.service``) or its
   container scope (``docker-<id>.scope`` / ``libpod-<id>.scope``), and the
   container id prefix-matches ``<runtime> ps`` output to a container name.
   The edge lives on the owning SERVICE CI and points at the PORT CI
   (``<host>:<port>``, the exact id ``collect_listening_ports`` emits), so a
   scan reads "service X connects_to its own listener endpoint". A listener
   that cannot be attributed confidently (no pid, unreadable cgroup, unit
   not loaded, container not listed) yields NO edge: an unattributed edge
   is a guess, and a guessed topology edge is worse than none.

2. Container network membership (:func:`collect_container_networks`).
   ``<runtime> network ls`` + ``<runtime> network inspect`` are read-only
   and map each container to the networks it sits on. Membership edges live
   on the container's SERVICE CI and point at a NETWORK CI named
   ``<host>:<network>`` (host-qualified because network names are
   host-local: two nodes' ``bridge`` are different networks). The default
   bridge IS edged like any other network: membership on it is still a
   shared-L2 fact, and dropping it would hide that a container was never
   segmented. Podman's lowercase ``containers`` key is accepted alongside
   Docker's ``Containers``.

There are deliberately NO cross-node edges here and no hook for them:
socket scraping across nodes is out of scope, and any future explicitly
approved cross-node source should arrive as its own reviewed collector.

Secret hygiene: the only attribute keys emitted are ``port``, ``bind``,
``proto``, ``process``, ``runtime``, ``network_name``, ``driver``,
``scope`` and ``origin``; none matches the CMDB secret-key fragment list
(``is_secret_attribute_key``), and no command output that could carry a
secret (labels, env) is stored.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from .cmdb import CIType, make_ci_id
from .discovery import (
    _SS_RE,
    DISCOVERED_TAG,
    CommandRunner,
    DiscoveredCI,
    _ephemeral_range,
)

logger = logging.getLogger("skcoord.discovery.connects")

#: ss process column: ``users:(("syncthing",pid=1234,fd=46))``. The pid is
#: what makes attribution possible; the process name alone is not evidence.
_SS_PROC_RE = re.compile(r'users:\(\("(?P<proc>[^"]+)"(?:,pid=(?P<pid>\d+))?')

#: systemd-cgroup unit shapes. ``user@<uid>.service`` is the per-user manager,
#: never the owning unit, so it is excluded and the inner unit wins instead.
_SERVICE_UNIT_RE = re.compile(r"^(?!user@\d+\.service$)(?P<unit>[^/]+)\.service$")

#: Container scopes written by docker, containerd and podman(libpod).
_CONTAINER_SCOPE_RE = re.compile(r"(?:docker|containerd|libpod)-(?P<cid>[0-9a-f]{12,64})\.scope$")


def _cgroup_paths(text: str) -> list[str]:
    """The cgroup path of each line, v1 (``1:name=systemd:/...``) or v2 (``0::/...``)."""
    return [line.rsplit(":", 1)[-1] for line in text.splitlines() if line.strip()]


def _unit_from_cgroup(text: str) -> Optional[str]:
    """The deepest ``*.service`` unit in a cgroup listing, or None.

    Deepest wins because a user-service process sits under
    ``user@1000.service/.../<real>.service`` and only the inner unit owns it.
    """
    unit = None
    for path in _cgroup_paths(text):
        for part in path.split("/"):
            match = _SERVICE_UNIT_RE.match(part)
            if match:
                unit = match.group("unit")
    return unit


def _container_id_from_cgroup(text: str) -> Optional[str]:
    """The container id from a docker/containerd/libpod scope, or None."""
    for path in _cgroup_paths(text):
        for part in reversed(path.split("/")):
            match = _CONTAINER_SCOPE_RE.search(part)
            if match:
                return match.group("cid")
    return None


def _observed_service_units(runner: CommandRunner) -> set[str]:
    """Loaded service unit names (no suffix), both scopes, matching the base collector."""
    units: set[str] = set()
    for scope in ("--user", "--system"):
        stdout = runner.run(
            [
                "systemctl",
                scope,
                "list-units",
                "--type=service",
                "--all",
                "--no-legend",
                "--no-pager",
                "--plain",
            ]
        )
        if stdout is None:
            continue
        for line in stdout.splitlines():
            match = re.match(r"^(?P<unit>[\w@:.\-]+)\.service\s+\S+\s+\S+\s+\S+", line.strip())
            if match and match.group("unit"):
                units.add(match.group("unit"))
    return units


def _observed_container_names(runner: CommandRunner) -> dict[str, str]:
    """Container short-id prefix -> name, per runtime, from read-only ``ps``."""
    names: dict[str, str] = {}
    for runtime in ("docker", "podman"):
        stdout = runner.run([runtime, "ps", "--format", "{{.ID}}\t{{.Names}}"])
        if not stdout:
            continue
        for line in stdout.splitlines():
            short_id, _, name = line.partition("\t")
            if short_id.strip() and name.strip():
                names[short_id.strip()] = name.strip()
    return names


def _container_for_cgroup_id(cgroup_id: str, containers: dict[str, str]) -> Optional[str]:
    """The container name whose short id prefixes the cgroup's full id, or None."""
    for short_id, name in containers.items():
        if cgroup_id.startswith(short_id):
            return name
    return None


def collect_port_service_links(runner: CommandRunner) -> list[DiscoveredCI]:
    """connects_to edges from an owning service CI to each listening port CI.

    Re-reads the same ``ss`` output the port collector uses (collectors stay
    independent; the pipeline folds the shared sightings), attributes every
    listener's pid through its cgroup, and emits one SERVICE sighting per
    attributable owner carrying the edges. Emits nothing for listeners with
    no confident owner.

    Args:
        runner: The host to inspect.

    Returns:
        Service-CI sightings whose only payload is the edge set; the scan
        merge unions them onto the full service CIs.
    """
    stdout = runner.run(["ss", "-tlnpH"])
    if stdout is None:
        stdout = runner.run(["ss", "-tlnH"])
    if stdout is None:
        return []
    lo, hi = _ephemeral_range(runner)

    listeners: list[tuple[int, int]] = []  # (port, pid)
    seen_ports: set[int] = set()
    for line in stdout.splitlines():
        match = _SS_RE.match(line.strip())
        if not match:
            continue
        _, _, port_text = match.group("local").rpartition(":")
        if not port_text.isdigit():
            continue
        port = int(port_text)
        if lo <= port <= hi or port in seen_ports:
            continue
        seen_ports.add(port)
        proc = _SS_PROC_RE.search(match.group("extra") or "")
        if proc and proc.group("pid"):
            listeners.append((port, int(proc.group("pid"))))

    if not listeners:
        # No ``ss -p`` permission (or no pids): partial coverage, stated in
        # the log, never guessed around.
        logger.info(
            "%s: no attributable listener pids (ss -p permission?); no port edges",
            runner.host,
        )
        return []

    units = _observed_service_units(runner)
    containers = _observed_container_names(runner)

    links: dict[str, set[str]] = {}
    for port, pid in listeners:
        cgroup = runner.run(["cat", f"/proc/{pid}/cgroup"])
        if not cgroup:
            continue
        owner = None
        cgroup_id = _container_id_from_cgroup(cgroup)
        if cgroup_id:
            owner = _container_for_cgroup_id(cgroup_id, containers)
        if owner is None:
            unit = _unit_from_cgroup(cgroup)
            if unit and unit in units:
                owner = unit
        if owner is None:
            continue
        links.setdefault(owner, set()).add(
            make_ci_id(CIType.PORT.value, f"{runner.host}:{port}")
        )

    return [
        DiscoveredCI(
            ci_type=CIType.SERVICE.value,
            name=owner,
            source="port-attribution",
            observed=True,
            node=runner.host,
            attributes={"origin": "port-attribution"},
            tags=(DISCOVERED_TAG,),
            relationships=tuple(("connects_to", target) for target in sorted(targets)),
        )
        for owner, targets in sorted(links.items())
    ]


def collect_container_networks(runner: CommandRunner) -> list[DiscoveredCI]:
    """NETWORK CIs plus container membership edges from ``network inspect``.

    Read-only throughout. A host with no container runtime returns nothing
    (the base docker collector makes the same explicit partial-coverage
    call). A network whose inspect output is malformed JSON, or not a list
    of objects, is skipped rather than guessed.

    Args:
        runner: The host to inspect.

    Returns:
        NETWORK CIs (one per inspected network) and SERVICE sightings for
        member containers carrying their ``connects_to`` network edges.
    """
    networks: list[DiscoveredCI] = []
    membership: dict[str, tuple[str, set[str]]] = {}
    for runtime in ("docker", "podman"):
        listing = runner.run([runtime, "network", "ls", "--format", "{{.Name}}"])
        if not listing:
            continue
        names = [line.strip() for line in listing.splitlines() if line.strip()]
        if not names:
            continue
        inspected = runner.run([runtime, "network", "inspect", *names])
        if not inspected:
            continue
        try:
            document = json.loads(inspected)
        except ValueError:
            logger.info("%s: %s network inspect returned malformed JSON", runner.host, runtime)
            continue
        if not isinstance(document, list):
            continue
        for entry in document:
            if not isinstance(entry, dict):
                continue
            # Docker uses "Containers"/"Name"; podman 4 lowercases both.
            network_name = entry.get("Name") or entry.get("name")
            if not network_name:
                continue
            network_name = str(network_name)
            network_ci_name = f"{runner.host}:{network_name}"
            networks.append(
                DiscoveredCI(
                    ci_type=CIType.NETWORK.value,
                    name=network_ci_name,
                    source=runtime,
                    observed=True,
                    node=runner.host,
                    attributes={
                        "network_name": network_name,
                        "driver": str(entry.get("Driver") or entry.get("driver") or ""),
                        "scope": str(entry.get("Scope") or entry.get("scope") or ""),
                        "runtime": runtime,
                        "origin": "container-network",
                    },
                    tags=("network", runtime, DISCOVERED_TAG),
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )
            containers = entry.get("Containers") or entry.get("containers") or {}
            if not isinstance(containers, dict):
                continue
            network_id = make_ci_id(CIType.NETWORK.value, network_ci_name)
            for info in containers.values():
                if not isinstance(info, dict):
                    continue
                container_name = info.get("Name") or info.get("name")
                if not container_name:
                    continue
                container = str(container_name)
                _, targets = membership.get(container, (runtime, set()))
                targets.add(network_id)
                membership[container] = (runtime, targets)

    members = [
        DiscoveredCI(
            ci_type=CIType.SERVICE.value,
            name=container,
            source=f"{member_runtime}-network",
            observed=True,
            node=runner.host,
            attributes={"origin": "container-network"},
            tags=(DISCOVERED_TAG,),
            relationships=tuple(("connects_to", target) for target in sorted(targets)),
        )
        for container, (member_runtime, targets) in sorted(membership.items())
    ]
    return networks + members


#: The connects_to collectors, registered into the scan in discovery.scan
#: (imported there to keep the module dependency one-directional).
CONNECTS_COLLECTORS = (collect_port_service_links, collect_container_networks)
