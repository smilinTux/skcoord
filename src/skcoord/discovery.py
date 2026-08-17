"""CMDB discovery: scan declared and observed state into the canonical CMDB.

``cmdb.seed_from_inventory()`` hardcoded three hostnames and scraped service
names out of ITIL incidents, so the CMDB could only ever describe the fleet
someone typed into it. This module replaces that with collectors over the real
sources, and keeps the one distinction that makes a CMDB worth having:

* **declared** state -- what a spec says should exist (fleet objects, the
  service registry, agent homes). Cheap, complete, and possibly fiction.
* **observed** state -- what is actually running on a machine right now
  (systemd units, listening sockets). Costs a command, cannot be stale.

A CMDB fed only declarations cannot tell you it is wrong. Every
:class:`DiscoveredCI` therefore carries ``source`` and ``observed``, and
:func:`drift` is the report that only exists because we kept them apart.

Machine access goes through a :class:`CommandRunner`, so the same collectors
run locally, over ssh against another node, or against a canned runner in a
test. Nothing here shells out on import, and :func:`reconcile` never deletes:
it creates, it updates, and it *reports* what it no longer sees.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from .cmdb import CIStatus, CIType, CMDBManager, make_ci_id

logger = logging.getLogger("skcoord.discovery")

DISCOVERED_TAG = "discovered"
"""Marks a CI as discovery-owned, so orphan handling never touches hand-made CIs."""

_DEFAULT_TIMEOUT = 20


# ---------------------------------------------------------------------------
# The unit of discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredCI:
    """One configuration item as some collector saw it.

    ``observed`` is the whole point: True means a machine was asked and
    answered, False means a file claimed it. Merging never lets a declaration
    overwrite an observation.
    """

    ci_type: str
    name: str
    source: str
    observed: bool = False
    node: str = ""
    description: str = ""
    attributes: dict = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    relationships: tuple[tuple[str, str], ...] = ()

    @property
    def ci_id(self) -> str:
        return make_ci_id(self.ci_type, self.name)

    def merged_with(self, other: "DiscoveredCI") -> "DiscoveredCI":
        """Fold another sighting of the same CI into this one.

        Observations win over declarations for the scalar fields; attributes
        and tags union, with the observed side taking precedence on conflict.
        """
        primary, secondary = (other, self) if (other.observed and not self.observed) else (self, other)
        attributes = {**secondary.attributes, **primary.attributes}
        sources = sorted({*self.source.split("+"), *other.source.split("+")})
        return replace(
            primary,
            source="+".join(sources),
            observed=self.observed or other.observed,
            node=primary.node or secondary.node,
            description=primary.description or secondary.description,
            attributes=attributes,
            tags=tuple(sorted({*self.tags, *other.tags})),
            relationships=tuple(sorted({*self.relationships, *other.relationships})),
        )


# ---------------------------------------------------------------------------
# Machine access
# ---------------------------------------------------------------------------


class CommandRunner(Protocol):
    """Runs a command somewhere and returns stdout, or None if it failed."""

    host: str

    def run(self, argv: Sequence[str]) -> Optional[str]: ...


@dataclass
class LocalRunner:
    """Runs commands on this machine."""

    host: str = ""
    timeout: int = _DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not self.host:
            import socket

            self.host = socket.gethostname()

    def run(self, argv: Sequence[str]) -> Optional[str]:
        return _exec(list(argv), self.timeout)


@dataclass
class SSHRunner:
    """Runs commands on another node over ssh.

    Used for the chi fleet and any node that is reachable but not part of this
    coordination home. BatchMode keeps a missing key a fast failure instead of
    a password prompt that hangs a cron job.
    """

    host: str
    target: str = ""
    timeout: int = _DEFAULT_TIMEOUT

    def run(self, argv: Sequence[str]) -> Optional[str]:
        remote = " ".join(shlex.quote(a) for a in argv)
        ssh = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={min(self.timeout, 10)}",
            self.target or self.host,
            remote,
        ]
        return _exec(ssh, self.timeout)


def _exec(argv: list[str], timeout: int) -> Optional[str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("command failed: %s (%s)", argv[0], exc)
        return None
    if proc.returncode != 0:
        logger.debug("command rc=%s: %s", proc.returncode, argv)
        return None
    return proc.stdout


# ---------------------------------------------------------------------------
# Declared-state collectors (read specs, no machine access)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collect_fleet_objects(home: Path) -> list[DiscoveredCI]:
    """Host / service CIs from the skfleet object model.

    ``~/.skcapstone/fleet/objects/{node,service,cronjob,operatorapp}`` is the
    fleet control plane's own declarative inventory, which makes it the closest
    thing to an authoritative asset list we already maintain.
    """
    out: list[DiscoveredCI] = []
    root = Path(home).expanduser() / "fleet" / "objects"

    for path in sorted((root / "node").glob("*.json")):
        obj = _read_json(path)
        if not obj:
            continue
        spec = obj.get("spec") or {}
        address = spec.get("address") or {}
        hostname = address.get("hostname") or obj.get("name") or path.stem
        attributes = {
            k: v
            for k, v in {
                "fleet_object": obj.get("name"),
                "role": spec.get("role"),
                "identity": spec.get("identity"),
                "cordoned": spec.get("cordoned"),
                "ip": address.get("ip"),
                "generation": obj.get("generation"),
            }.items()
            if v is not None
        }
        out.append(
            DiscoveredCI(
                ci_type=CIType.HOST.value,
                name=hostname,
                source="fleet:node",
                node=hostname,
                description=f"fleet node {obj.get('name', '')}".strip(),
                attributes=attributes,
                tags=("fleet", DISCOVERED_TAG),
            )
        )

    for kind, tag in (("service", "service"), ("cronjob", "cronjob"), ("operatorapp", "app")):
        for path in sorted((root / kind).glob("*.json")):
            obj = _read_json(path)
            if not obj:
                continue
            spec = obj.get("spec") or {}
            name = obj.get("name") or path.stem
            attributes = {
                k: v
                for k, v in {
                    "runtime": spec.get("runtime"),
                    "schedule": obj.get("schedule"),
                    "restart_policy": spec.get("restartPolicy"),
                    "failover": spec.get("failover"),
                    "tier": (obj.get("labels") or {}).get("tier"),
                    "generation": obj.get("generation"),
                }.items()
                if v is not None
            }
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=name,
                    source=f"fleet:{kind}",
                    description=(spec.get("note") or "")[:200],
                    attributes=attributes,
                    tags=("fleet", tag, DISCOVERED_TAG),
                )
            )
    return out


def collect_registry(home: Path) -> list[DiscoveredCI]:
    """Service CIs from ``~/.skcapstone/registry``.

    Services self-register here at startup, which reads like observation but is
    not: nothing removes the file when a service dies, so a registry entry is a
    *claim with a timestamp*. It stays declared, and ``registered_at`` is kept
    so staleness is visible rather than assumed away.
    """
    out: list[DiscoveredCI] = []
    root = Path(home).expanduser() / "registry"
    for path in sorted(root.glob("*.json")):
        obj = _read_json(path)
        if not obj:
            continue
        name = obj.get("name") or path.stem
        attributes = {
            k: v
            for k, v in {
                "health_url": obj.get("health_url"),
                "registered_at": obj.get("registered_at"),
                "registered_by": obj.get("registered_by"),
                "pid_file": obj.get("pid_file"),
            }.items()
            if v is not None
        }
        out.append(
            DiscoveredCI(
                ci_type=CIType.SERVICE.value,
                name=name,
                source="registry",
                attributes=attributes,
                tags=("registry", DISCOVERED_TAG),
            )
        )
    return out


def collect_agents(home: Path) -> list[DiscoveredCI]:
    """Agent CIs from the per-agent homes under ``~/.skcapstone/agents``."""
    out: list[DiscoveredCI] = []
    root = Path(home).expanduser() / "agents"
    if not root.is_dir():
        return out
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        soul = path / "soul" / "base.json"
        attributes: dict[str, Any] = {"home": str(path)}
        blueprint = _read_json(soul) if soul.exists() else None
        if blueprint:
            for key in ("name", "role", "version"):
                if blueprint.get(key):
                    attributes[f"soul_{key}"] = blueprint[key]
        out.append(
            DiscoveredCI(
                ci_type=CIType.AGENT.value,
                name=path.name,
                source="agents",
                attributes=attributes,
                tags=("agent", DISCOVERED_TAG),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Observed-state collectors (ask a machine)
# ---------------------------------------------------------------------------

_UNIT_RE = re.compile(r"^(?P<unit>[\w@:.\\-]+)\.service\s+(?P<load>\S+)\s+(?P<active>\S+)\s+(?P<sub>\S+)")


def collect_systemd_units(
    runner: CommandRunner, scopes: Sequence[str] = ("--user", "--system")
) -> list[DiscoveredCI]:
    """Service CIs for units actually loaded on ``runner``'s host.

    Both scopes are collected because the sk* fleet runs services under
    ``systemctl --user`` while the platform pieces they depend on are system
    units. Querying only one scope is how a node looks half-empty.
    """
    out: list[DiscoveredCI] = []
    for scope in scopes:
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
            logger.debug("systemd scope %s unavailable on %s", scope, runner.host)
            continue
        for line in stdout.splitlines():
            match = _UNIT_RE.match(line.strip())
            if not match:
                continue
            unit = match.group("unit")
            active = match.group("active")
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=unit,
                    source=f"systemd{scope}",
                    observed=True,
                    node=runner.host,
                    attributes={
                        "systemd_scope": scope.lstrip("-"),
                        "load_state": match.group("load"),
                        "active_state": active,
                        "sub_state": match.group("sub"),
                    },
                    tags=("systemd", DISCOVERED_TAG),
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )
    return out


# ss -tlnpH columns: State Recv-Q Send-Q Local:Port Peer:Port [Process]
_SS_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<extra>.*))?$")
_PROC_RE = re.compile(r'users:\(\("(?P<proc>[^"]+)"')


def collect_listening_ports(runner: CommandRunner) -> list[DiscoveredCI]:
    """Port CIs for TCP sockets in LISTEN on ``runner``'s host.

    This is the collector that catches the service nobody declared: a port open
    with no matching fleet object is either undocumented infrastructure or
    something that should not be running.
    """
    stdout = runner.run(["ss", "-tlnpH"])
    if stdout is None:
        stdout = runner.run(["ss", "-tlnH"])
    if stdout is None:
        return []

    out: list[DiscoveredCI] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        match = _SS_RE.match(line.strip())
        if not match:
            continue
        local = match.group("local")
        _, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        bind = local[: -(len(port) + 1)] or "*"
        name = f"{runner.host}:{port}"
        if name in seen:
            continue
        seen.add(name)
        attributes: dict[str, Any] = {"port": int(port), "bind": bind, "proto": "tcp"}
        proc = _PROC_RE.search(match.group("extra") or "")
        if proc:
            attributes["process"] = proc.group("proc")
        out.append(
            DiscoveredCI(
                ci_type=CIType.PORT.value,
                name=name,
                source="ss",
                observed=True,
                node=runner.host,
                attributes=attributes,
                tags=("port", DISCOVERED_TAG),
                relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
            )
        )
    return out


def collect_host_facts(runner: CommandRunner) -> list[DiscoveredCI]:
    """A host CI for the machine ``runner`` points at, with basic facts."""
    attributes: dict[str, Any] = {}
    uname = runner.run(["uname", "-sr"])
    if uname:
        attributes["kernel"] = uname.strip()
    nproc = runner.run(["nproc"])
    if nproc and nproc.strip().isdigit():
        attributes["cores"] = int(nproc.strip())
    if not attributes:
        return []
    return [
        DiscoveredCI(
            ci_type=CIType.HOST.value,
            name=runner.host,
            source="host",
            observed=True,
            node=runner.host,
            attributes=attributes,
            tags=("fleet", DISCOVERED_TAG),
        )
    ]


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

DECLARED_COLLECTORS: tuple[Callable[[Path], list[DiscoveredCI]], ...] = (
    collect_fleet_objects,
    collect_registry,
    collect_agents,
)

OBSERVED_COLLECTORS: tuple[Callable[[CommandRunner], list[DiscoveredCI]], ...] = (
    collect_host_facts,
    collect_systemd_units,
    collect_listening_ports,
)


def merge(items: Iterable[DiscoveredCI]) -> list[DiscoveredCI]:
    """Fold multiple sightings of one CI into a single record, by ci_id."""
    folded: dict[str, DiscoveredCI] = {}
    for item in items:
        existing = folded.get(item.ci_id)
        folded[item.ci_id] = existing.merged_with(item) if existing else item
    return sorted(folded.values(), key=lambda c: (c.ci_type, c.name))


def scan(
    home: Path,
    runners: Sequence[CommandRunner] = (),
    include_declared: bool = True,
) -> list[DiscoveredCI]:
    """Run every collector and fold the results.

    ``runners`` is empty by default so a scan is never implicitly a scan of
    *this* box: the caller says which machines to ask.
    """
    found: list[DiscoveredCI] = []
    if include_declared:
        for collector in DECLARED_COLLECTORS:
            try:
                found.extend(collector(Path(home)))
            except Exception:  # noqa: BLE001 - one bad source must not blind the rest
                logger.exception("declared collector failed: %s", collector.__name__)
    for runner in runners:
        for observer in OBSERVED_COLLECTORS:
            try:
                found.extend(observer(runner))
            except Exception:  # noqa: BLE001
                logger.exception("observed collector failed: %s on %s", observer.__name__, runner.host)
    return merge(found)


# ---------------------------------------------------------------------------
# Reconcile + drift
# ---------------------------------------------------------------------------


@dataclass
class ReconcileReport:
    """What a reconcile did, or would do when ``applied`` is False."""

    applied: bool = False
    created: list[str] = field(default_factory=list)
    updated: dict[str, list[str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "orphans": self.orphans,
            "counts": {
                "created": len(self.created),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "orphans": len(self.orphans),
            },
        }


def reconcile(
    mgr: CMDBManager,
    discovered: Sequence[DiscoveredCI],
    agent: str = "cmdb-discovery",
    apply: bool = False,
) -> ReconcileReport:
    """Converge the CMDB on what was discovered. Additive only.

    Nothing is deleted, ever. A CI that discovery no longer sees is reported as
    an orphan and left alone: the store is event-sourced and shared, so a
    collector that silently failed must not be able to erase inventory.
    """
    report = ReconcileReport(applied=apply)
    by_id = {d.ci_id: d for d in discovered}

    existing = {ci.id: ci for ci in mgr.list_cis()}

    for ci_id, item in by_id.items():
        current = existing.get(ci_id)
        if current is None:
            report.created.append(ci_id)
            if apply:
                mgr.create_ci(
                    item.name,
                    item.ci_type,
                    description=item.description,
                    node=item.node,
                    attributes=dict(item.attributes),
                    tags=list(item.tags),
                    ci_id=ci_id,
                )
                for rel_type, target in item.relationships:
                    mgr.add_relationship(ci_id, agent, rel_type, target)
            continue

        changed = [
            key
            for key, value in item.attributes.items()
            if current.attributes.get(key) != value
        ]
        missing_rels = [
            (rel_type, target)
            for rel_type, target in item.relationships
            if not any(r.rel_type == rel_type and r.target == target for r in current.relationships)
        ]
        if not changed and not missing_rels:
            report.unchanged.append(ci_id)
            continue

        report.updated[ci_id] = sorted(changed) + [f"{r}->{t}" for r, t in missing_rels]
        if apply:
            for key in changed:
                mgr.set_attribute(ci_id, agent, key, item.attributes[key])
            for rel_type, target in missing_rels:
                mgr.add_relationship(ci_id, agent, rel_type, target)

    for ci_id, ci in existing.items():
        if ci_id in by_id:
            continue
        if DISCOVERED_TAG in (ci.tags or []) and ci.status != CIStatus.RETIRED.value:
            report.orphans.append(ci_id)

    return report


@dataclass
class DriftFinding:
    ci_id: str
    kind: str
    detail: str

    def as_dict(self) -> dict:
        return {"ci_id": self.ci_id, "kind": self.kind, "detail": self.detail}


def drift(discovered: Sequence[DiscoveredCI], mgr: Optional[CMDBManager] = None) -> list[DriftFinding]:
    """Where declaration and observation disagree.

    Three findings, in the order they tend to matter:

    * ``declared_not_observed`` -- a spec says this service exists and no
      machine is running it. The fleet is not what the manifest claims.
    * ``observed_not_declared`` -- something is listening or running that no
      spec mentions. Undocumented, and possibly unwanted.
    * ``stored_not_discovered`` -- the CMDB holds a discovery-owned CI that no
      collector saw this pass. Usually a decommission nobody recorded.
    """
    findings: list[DriftFinding] = []
    services = [d for d in discovered if d.ci_type == CIType.SERVICE.value]

    observed_names = {_service_key(d.name) for d in services if d.observed}
    for item in services:
        if item.observed:
            continue
        if _service_key(item.name) not in observed_names:
            findings.append(
                DriftFinding(
                    item.ci_id,
                    "declared_not_observed",
                    f"declared by {item.source}, no running unit matched",
                )
            )

    declared_names = {_service_key(d.name) for d in services if not d.observed}
    for item in services:
        if not item.observed:
            continue
        if _service_key(item.name) not in declared_names:
            findings.append(
                DriftFinding(
                    item.ci_id,
                    "observed_not_declared",
                    f"running on {item.node or 'unknown'}, no fleet object or registry entry",
                )
            )

    if mgr is not None:
        seen = {d.ci_id for d in discovered}
        for ci in mgr.list_cis():
            if ci.id in seen:
                continue
            if DISCOVERED_TAG in (ci.tags or []) and ci.status != CIStatus.RETIRED.value:
                findings.append(
                    DriftFinding(ci.id, "stored_not_discovered", "in CMDB, not seen this scan")
                )

    return findings


def _service_key(name: str) -> str:
    """Normalise a service name so 'skgateway' and 'skgateway.service' match."""
    key = name.strip().lower()
    for suffix in (".service", ".timer"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key.replace("_", "-")
