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
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from .cmdb import CIStatus, CIType, CMDBManager, make_ci_id

logger = logging.getLogger("skcoord.discovery")

DISCOVERED_TAG = "discovered"
"""Marks a CI as discovery-owned, so orphan handling never touches hand-made CIs."""

_DEFAULT_TIMEOUT = 20

AUTHORITY_DECLARED = "declared"
AUTHORITY_OBSERVED = "observed"
OBSERVATION_SCHEMA_VERSION = 1


class ObservationState(str, Enum):
    """Evidence quality, deliberately independent from CI health."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


def observation_state(
    observed_at: str, *, now: Optional[datetime] = None, max_age: timedelta = timedelta(hours=6)
) -> ObservationState:
    if not observed_at:
        return ObservationState.UNKNOWN
    try:
        timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return ObservationState.UNKNOWN
    current = now or datetime.now(timezone.utc)
    return ObservationState.FRESH if current - timestamp <= max_age else ObservationState.STALE


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
    declared_flag: Optional[bool] = None
    canonical_name: str = ""
    aliases: tuple[str, ...] = ()
    observed_at: str = ""
    scan_id: str = ""
    authority: str = ""
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    @property
    def ci_id(self) -> str:
        return make_ci_id(self.ci_type, self.canonical_name or self.name)

    @property
    def declared(self) -> bool:
        """Whether a spec claims this CI exists.

        Kept separate from ``observed`` because the healthy case is *both*, and
        a merge that collapsed them into one flag made every running,
        correctly-declared service look undocumented.
        """
        return (not self.observed) if self.declared_flag is None else self.declared_flag

    def merged_with(self, other: "DiscoveredCI") -> "DiscoveredCI":
        """Fold another sighting of the same CI into this one.

        Observations win over declarations for the scalar fields; attributes
        and tags union, with the observed side taking precedence on conflict.
        Both provenance flags survive: the result is declared *and* observed.
        """
        primary, secondary = (
            (other, self) if (other.observed and not self.observed) else (self, other)
        )
        if self.declared and not other.declared:
            canonical_name = self.canonical_name or self.name
        elif other.declared and not self.declared:
            canonical_name = other.canonical_name or other.name
        else:
            canonical_name = min(
                filter(None, (self.canonical_name or self.name, other.canonical_name or other.name))
            )
        attributes = {**secondary.attributes, **primary.attributes}
        sources = sorted({*self.source.split("+"), *other.source.split("+")})
        return replace(
            primary,
            source="+".join(sources),
            observed=self.observed or other.observed,
            declared_flag=self.declared or other.declared,
            node=primary.node or secondary.node,
            description=primary.description or secondary.description,
            attributes=attributes,
            tags=tuple(sorted({*self.tags, *other.tags})),
            relationships=tuple(sorted({*self.relationships, *other.relationships})),
            canonical_name=canonical_name,
            aliases=tuple(sorted({*self.identity_aliases, *other.identity_aliases})),
            observed_at=max(self.observed_at, other.observed_at),
            scan_id=primary.scan_id or secondary.scan_id,
            authority=(AUTHORITY_OBSERVED if self.observed or other.observed else AUTHORITY_DECLARED),
        )

    @property
    def identity_aliases(self) -> tuple[str, ...]:
        """Normalised names by which this CI may be recognised."""
        values = {self.name, self.canonical_name, *self.aliases}
        return tuple(sorted({_normalise_alias(v) for v in values if v}))


def _normalise_alias(value: str) -> str:
    return str(value).strip().lower().rstrip(".")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                canonical_name=hostname,
                aliases=tuple(
                    str(v) for v in (obj.get("name"), address.get("ip"), address.get("ssh_alias")) if v
                ),
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


def device_from_fingerprint(
    name: str,
    source: str,
    *,
    aliases: Sequence[str] = (),
    attributes: Optional[dict] = None,
    observed_at: str = "",
    scan_id: str = "",
) -> DiscoveredCI:
    """Represent an observed, non-managed appliance without calling it a host.

    A fingerprint proves presence, not fleet ownership or manageability.  Such
    assets use ``device`` until a fleet Node declaration supplies the stronger
    host lifecycle; aliases allow later reconciliation without an ID rewrite.
    """
    return DiscoveredCI(
        ci_type=CIType.DEVICE.value,
        name=name,
        canonical_name=name,
        aliases=tuple(aliases),
        source=source,
        observed=True,
        observed_at=observed_at,
        scan_id=scan_id,
        authority=AUTHORITY_OBSERVED,
        attributes={"managed": False, **(attributes or {})},
        tags=("device", "unmanaged", "fingerprint-only", DISCOVERED_TAG),
    )


# ---------------------------------------------------------------------------
# Observed-state collectors (ask a machine)
# ---------------------------------------------------------------------------

_UNIT_RE = re.compile(
    r"^(?P<unit>[\w@:.\\-]+)\.(?P<kind>service|timer)\s+(?P<load>\S+)\s+(?P<active>\S+)\s+(?P<sub>\S+)"
)

ORIGIN_OPERATOR = "operator"
ORIGIN_DISTRO = "distro"
ORIGIN_UNKNOWN = "unknown"

_DISTRO_UNIT_DIRS = ("/usr/lib/systemd/", "/lib/systemd/", "/usr/local/lib/systemd/")


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


def _fragment_paths(runner: CommandRunner, scope: str, units: Sequence[str]) -> dict[str, str]:
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
            ["systemctl", scope, "show", *chunk, "-p", "Id", "-p", "FragmentPath", "--no-pager"]
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
                logger.debug("systemd %s/%s unavailable on %s", scope, kind, runner.host)
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

        paths = _fragment_paths(runner, scope, [f"{unit}.{kind}" for unit, kind, *_ in rows])

        for unit, kind, load, active, sub in rows:
            fragment = paths.get(f"{unit}.{kind}", "")
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
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )
    return out


def collect_docker_containers(runner: CommandRunner) -> list[DiscoveredCI]:
    """Service CIs for running containers.

    Several fleet services declare ``runtime: docker``. Without this collector
    they are declared, never observed, and drift reports them as missing when
    they are running perfectly well.
    """
    stdout = runner.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"]
    )
    if not stdout:
        return []
    out: list[DiscoveredCI] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        attributes: dict[str, Any] = {"runtime": "docker", "origin": "container"}
        if len(parts) > 1 and parts[1].strip():
            attributes["image"] = parts[1].strip()
        if len(parts) > 2 and parts[2].strip():
            attributes["container_status"] = parts[2].strip()
        if len(parts) > 3 and parts[3].strip():
            attributes["container_ports"] = parts[3].strip()
        out.append(
            DiscoveredCI(
                ci_type=CIType.SERVICE.value,
                name=name,
                source="docker",
                observed=True,
                node=runner.host,
                attributes=attributes,
                tags=("docker", DISCOVERED_TAG),
                relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
            )
        )
    return out


# ss -tlnpH columns: State Recv-Q Send-Q Local:Port Peer:Port [Process]
_SS_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?:\s+(?P<extra>.*))?$")
_PROC_RE = re.compile(r'users:\(\("(?P<proc>[^"]+)"')


_FALLBACK_EPHEMERAL = (32768, 60999)


def _ephemeral_range(runner: CommandRunner) -> tuple[int, int]:
    """The host's ephemeral port range, read from the host itself.

    Read rather than hardcoded because a tuned node moves it, and the range is
    asked of ``runner`` so a remote scan uses the remote host's range instead
    of this one's.
    """
    stdout = runner.run(["cat", "/proc/sys/net/ipv4/ip_local_port_range"])
    if stdout:
        parts = stdout.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return int(parts[0]), int(parts[1])
    return _FALLBACK_EPHEMERAL


def collect_listening_ports(runner: CommandRunner) -> list[DiscoveredCI]:
    """Port CIs for TCP sockets in LISTEN on ``runner``'s host.

    This is the collector that catches the service nobody declared: a port open
    with no matching fleet object is either undocumented infrastructure or
    something that should not be running.

    Ports inside the host's ephemeral range are skipped. A listener there is
    transient by definition (RPC, mDNS, tailscale, a short-lived python
    server), and the CMDB is append-only: at one scan every three hours, each
    reboot's fresh set of random high ports would accrete as permanent CIs
    forever while the previous set turned into permanent orphans. The count of
    skipped ports is logged rather than dropped in silence, so this reads as a
    deliberate exclusion and not as a host with fewer ports than it has.
    """
    stdout = runner.run(["ss", "-tlnpH"])
    if stdout is None:
        stdout = runner.run(["ss", "-tlnH"])
    if stdout is None:
        return []

    lo, hi = _ephemeral_range(runner)
    skipped = 0

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
        if lo <= int(port) <= hi:
            skipped += 1
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
    if skipped:
        logger.info(
            "%s: skipped %d listening port(s) in the ephemeral range %d-%d",
            runner.host,
            skipped,
            lo,
            hi,
        )
    return out


def collect_host_facts(runner: CommandRunner) -> list[DiscoveredCI]:
    """A host CI with normalised compute, memory, disk and address facts.

    Every command is optional.  Failed or malformed commands leave facts
    absent; they never manufacture zero capacity.  ``fact_provenance`` keeps
    the exact successful output so an operator can audit normalisation.
    """
    attributes: dict[str, Any] = {}
    raw: dict[str, str] = {}

    def ask(key: str, argv: list[str]) -> Optional[str]:
        value = runner.run(argv)
        if value and value.strip():
            raw[key] = value.strip()
            return value
        return None

    uname = ask("uname", ["uname", "-sr"])
    if uname:
        attributes["kernel"] = uname.strip()
    nproc = ask("nproc", ["nproc"])
    if nproc and nproc.strip().isdigit():
        attributes["cpu_logical"] = int(nproc.strip())
        attributes["cores"] = int(nproc.strip())  # backwards-compatible v1 fact name

    lscpu = ask("lscpu", ["lscpu", "-J"])
    if lscpu:
        try:
            rows = json.loads(lscpu).get("lscpu", [])
            cpu = {str(row.get("field", "")).rstrip(":"): row.get("data") for row in rows}
            for source, target in (
                ("Socket(s)", "cpu_sockets"),
                ("Core(s) per socket", "cpu_cores_per_socket"),
                ("Thread(s) per core", "cpu_threads_per_core"),
                ("CPU(s)", "cpu_logical"),
            ):
                value = str(cpu.get(source, ""))
                if value.isdigit():
                    attributes[target] = int(value)
                    if target == "cpu_logical":
                        attributes["cores"] = int(value)
            if cpu.get("Model name"):
                attributes["cpu_model"] = str(cpu["Model name"])
        except (TypeError, ValueError):
            pass

    memory = ask("memory", ["free", "-b"])
    if memory:
        for line in memory.splitlines():
            parts = line.split()
            if parts and parts[0].rstrip(":").lower() == "mem" and len(parts) >= 7:
                numeric = parts[1:7]
                if all(v.isdigit() for v in numeric):
                    for key, value in zip(
                        ("memory_total_bytes", "memory_used_bytes", "memory_free_bytes",
                         "memory_shared_bytes", "memory_cache_bytes", "memory_available_bytes"),
                        numeric,
                    ):
                        attributes[key] = int(value)

    addresses = ask("addresses", ["ip", "-j", "address", "show"])
    aliases: set[str] = {runner.host}
    if addresses:
        try:
            parsed = json.loads(addresses)
            ips = sorted(
                {
                    info["local"]
                    for iface in parsed
                    if iface.get("ifname") != "lo"
                    for info in iface.get("addr_info", [])
                    if info.get("local") and info.get("scope") in ("global", "site")
                }
            )
            if ips:
                attributes["ip_addresses"] = ips
                aliases.update(ips)
        except (TypeError, ValueError, KeyError):
            pass

    disks = ask(
        "block_devices",
        ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS"],
    )
    if disks:
        try:
            devices = json.loads(disks).get("blockdevices", [])
            attributes["block_devices"] = devices
            sizes = [d.get("size") for d in devices if d.get("type") == "disk"]
            if sizes and all(isinstance(v, int) for v in sizes):
                attributes["disk_capacity_bytes"] = sum(sizes)
        except (TypeError, ValueError):
            pass

    usage = ask(
        "filesystems",
        ["df", "-B1", "--output=source,size,used,avail,pcent,target"],
    )
    if usage:
        filesystems = []
        for line in usage.splitlines()[1:]:
            parts = line.split(None, 5)
            if len(parts) == 6 and all(v.isdigit() for v in parts[1:4]):
                filesystems.append(
                    {"source": parts[0], "size_bytes": int(parts[1]),
                     "used_bytes": int(parts[2]), "available_bytes": int(parts[3]),
                     "used_percent": parts[4], "mountpoint": parts[5]}
                )
        if filesystems:
            attributes["filesystems"] = filesystems

    if not attributes:
        return []
    attributes["fact_provenance"] = raw
    return [
        DiscoveredCI(
            ci_type=CIType.HOST.value,
            name=runner.host,
            source="host",
            observed=True,
            node=runner.host,
            attributes=attributes,
            tags=("fleet", DISCOVERED_TAG),
            canonical_name=runner.host,
            aliases=tuple(sorted(aliases)),
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
    collect_docker_containers,
    collect_listening_ports,
)


def merge(items: Iterable[DiscoveredCI]) -> list[DiscoveredCI]:
    """Fold sightings, including host aliases, into deterministic records."""
    folded: list[DiscoveredCI] = []
    for item in items:
        matches = [
            existing
            for existing in folded
            if existing.ci_type == item.ci_type
            and (
                existing.ci_id == item.ci_id
                or (
                    item.ci_type in (CIType.HOST.value, CIType.DEVICE.value)
                    and set(existing.identity_aliases) & set(item.identity_aliases)
                )
            )
        ]
        if not matches:
            folded.append(item)
        else:
            merged = item
            for match in matches:
                merged = match.merged_with(merged)
                folded.remove(match)
            folded.append(merged)
    return sorted(folded, key=lambda c: (c.ci_type, c.canonical_name or c.name))


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
    scan_id = uuid.uuid4().hex
    observed_at = _utc_now()
    if include_declared:
        for collector in DECLARED_COLLECTORS:
            try:
                found.extend(
                    replace(
                        item,
                        scan_id=item.scan_id or scan_id,
                        authority=item.authority or AUTHORITY_DECLARED,
                    )
                    for item in collector(Path(home))
                )
            except Exception:  # noqa: BLE001 - one bad source must not blind the rest
                logger.exception("declared collector failed: %s", collector.__name__)
    for runner in runners:
        for observer in OBSERVED_COLLECTORS:
            try:
                observations = observer(runner)
                found.extend(
                    replace(
                        item,
                        observed_at=item.observed_at or observed_at,
                        scan_id=item.scan_id or scan_id,
                        authority=item.authority or AUTHORITY_OBSERVED,
                    )
                    for item in observations
                )
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


def _observed_status(item: DiscoveredCI) -> Optional[str]:
    """CIStatus a collector's observation implies, or None.

    Precedence rule (written down, CMDB-6): for discovery-owned CIs the
    observed systemd state IS the health. ``active_state=active`` reads
    operational, ``failed`` reads down. Every other systemd state (inactive,
    activating, ...) is ambiguous in service terms (a oneshot or timer is
    legitimately inactive), so we do not force a status for it and leave any
    existing value alone. ITIL incident severity is informational and is
    surfaced via ``impact_analysis().open_incidents``; it never overrides an
    observed state on the CIStatus field.
    """
    if not item.observed or item.ci_type != CIType.SERVICE.value:
        return None
    state = item.attributes.get("active_state")
    if state == "active":
        return CIStatus.OPERATIONAL.value
    if state == "failed":
        return CIStatus.DOWN.value
    return None


def reconcile(
    mgr: CMDBManager,
    discovered: Sequence[DiscoveredCI],
    agent: str = "cmdb-discovery",
    apply: bool = False,
    scan_complete: bool = True,
) -> ReconcileReport:
    """Converge the CMDB on what was discovered. Additive only.

    Nothing is deleted, ever. A CI that discovery no longer sees is reported as
    an orphan and left alone: the store is event-sourced and shared, so a
    collector that silently failed must not be able to erase inventory.
    Callers must pass ``scan_complete=False`` when any required collector or
    target failed; absence from a partial scan then produces no orphan signal.

    Status precedence (CMDB-6): CIStatus on a discovery-owned CI is derived
    from the observed state, so a CI can never read ``degraded`` while its own
    ``active_state`` attribute says ``active``. An ITIL incident's severity is
    its own signal (see ``impact_analysis``); it does not get folded into the
    headline CIStatus. A manually retired CI is never un-retired by reconcile.
    """
    report = ReconcileReport(applied=apply)
    existing = {ci.id: ci for ci in mgr.list_cis()}
    by_id: dict[str, DiscoveredCI] = {}
    migrations: list[tuple[str, str]] = []
    for item in discovered:
        ci_id = item.ci_id
        if item.ci_type in (CIType.HOST.value, CIType.DEVICE.value):
            aliases = set(item.identity_aliases)
            matching = [
                ci
                for ci in existing.values()
                if ci.ci_type in (CIType.HOST.value, CIType.DEVICE.value)
                and aliases
                & {
                    _normalise_alias(value)
                    for value in (
                        ci.id.removeprefix(f"ci-{ci.ci_type}-"),
                        ci.name,
                        ci.attributes.get("canonical_name", ""),
                        *ci.attributes.get("aliases", []),
                    )
                    if value
                }
            ]
            if matching:
                target = existing.get(ci_id) or sorted(matching, key=lambda ci: ci.id)[0]
                ci_id = target.id
                migrations.extend(
                    (duplicate.id, target.id)
                    for duplicate in matching
                    if duplicate.id != target.id
                )
        by_id[ci_id] = item

    for ci_id, item in by_id.items():
        current = existing.get(ci_id)
        derived_status = _observed_status(item)
        if current is None:
            # A brand-new CI has no status events yet; core.json defaults to
            # operational. Only an explicit DOWN/Failed write is needed, and only
            # for observed discoveries.
            report.created.append(ci_id)
            if apply:
                attributes = {
                    **item.attributes,
                    "canonical_name": item.canonical_name or item.name,
                    "aliases": list(item.identity_aliases),
                    "source_authority": item.authority
                    or (AUTHORITY_OBSERVED if item.observed else AUTHORITY_DECLARED),
                    "observation_schema_version": item.schema_version,
                }
                if item.observed_at:
                    attributes.update(
                        {
                            "observed_at": item.observed_at,
                            "scan_id": item.scan_id,
                            "observation_state": observation_state(item.observed_at).value,
                        }
                    )
                mgr.create_ci(
                    item.name,
                    item.ci_type,
                    description=item.description,
                    node=item.node,
                    attributes=attributes,
                    tags=list(item.tags),
                    ci_id=ci_id,
                )
                if derived_status and derived_status != CIStatus.OPERATIONAL.value:
                    mgr.set_status(
                        ci_id, agent, derived_status, note="from observed active_state"
                    )
                for rel_type, target in item.relationships:
                    mgr.add_relationship(ci_id, agent, rel_type, target)
            continue

        evidence = {
            "canonical_name": item.canonical_name or item.name,
            "aliases": list(item.identity_aliases),
            "source_authority": item.authority
            or (AUTHORITY_OBSERVED if item.observed else AUTHORITY_DECLARED),
            "observation_schema_version": item.schema_version,
        }
        if item.observed_at:
            evidence.update(
                {
                    "observed_at": item.observed_at,
                    "scan_id": item.scan_id,
                    "observation_state": observation_state(item.observed_at).value,
                }
            )
        desired_attributes = {**item.attributes, **evidence}
        changed = [
            key
            for key, value in desired_attributes.items()
            if current.attributes.get(key) != value
        ]
        missing_rels = [
            (rel_type, target)
            for rel_type, target in item.relationships
            if not any(r.rel_type == rel_type and r.target == target for r in current.relationships)
        ]
        status_changes = (
            derived_status is not None
            and current.status != CIStatus.RETIRED.value
            and current.status != derived_status
        )
        if not changed and not missing_rels and not status_changes:
            report.unchanged.append(ci_id)
            continue

        report_changes = sorted(changed)
        if status_changes:
            report_changes = ["status"] + report_changes
        report.updated[ci_id] = report_changes + [f"{r}->{t}" for r, t in missing_rels]
        if apply:
            for key in changed:
                mgr.set_attribute(ci_id, agent, key, desired_attributes[key])
            if status_changes:
                mgr.set_status(ci_id, agent, derived_status, note="from observed active_state")
            for rel_type, target in missing_rels:
                mgr.add_relationship(ci_id, agent, rel_type, target)

    if not scan_complete:
        return report

    for ci_id, ci in existing.items():
        if ci_id in by_id:
            continue
        if any(duplicate_id == ci_id for duplicate_id, _ in migrations):
            continue
        if DISCOVERED_TAG in (ci.tags or []) and ci.status != CIStatus.RETIRED.value:
            report.orphans.append(ci_id)

    for duplicate_id, canonical_id in sorted(set(migrations)):
        duplicate = existing[duplicate_id]
        relation = f"alias_of->{canonical_id}"
        if any(
            rel.rel_type == "alias_of" and rel.target == canonical_id
            for rel in duplicate.relationships
        ):
            continue
        report.updated.setdefault(duplicate_id, []).append(relation)
        if apply:
            mgr.add_relationship(duplicate_id, agent, "alias_of", canonical_id)

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
    * ``observed_not_declared`` -- something is running that no spec mentions.
      Undocumented, and possibly unwanted. Distro-authored systemd units are
      excluded: ``ModemManager.service`` is not an asset anyone forgot to
      declare, and 300 of them bury the 20 findings that matter. Units whose
      origin could not be determined are still reported, so a failed
      ``FragmentPath`` lookup shows up as noise rather than as silence.
    * ``stored_not_discovered`` -- the CMDB holds a discovery-owned CI that no
      collector saw this pass. Usually a decommission nobody recorded.
    """
    findings: list[DriftFinding] = []
    services = [d for d in discovered if d.ci_type == CIType.SERVICE.value]

    # Flags cover the merged case (one CI both declared and observed); the key
    # sets cover records that never merged because their names differ, e.g. a
    # spec saying "skgateway" against a unit called "skgateway.service".
    observed_names = {_service_key(d.name) for d in services if d.observed}
    declared_names = {_service_key(d.name) for d in services if d.declared}

    for item in services:
        if item.observed or not item.declared:
            continue
        if _service_key(item.name) not in observed_names:
            findings.append(
                DriftFinding(
                    item.ci_id,
                    "declared_not_observed",
                    f"declared by {item.source}, no running unit, timer or container matched",
                )
            )

    for item in services:
        if not item.observed or item.declared:
            continue
        if item.attributes.get("origin") == ORIGIN_DISTRO:
            continue
        if _service_key(item.name) not in declared_names:
            findings.append(
                DriftFinding(
                    item.ci_id,
                    "observed_not_declared",
                    f"running on {item.node or 'unknown'} "
                    f"(origin={item.attributes.get('origin', ORIGIN_UNKNOWN)}), "
                    "no fleet object or registry entry",
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
