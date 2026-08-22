"""Observed host, port, cron, and network discovery collectors."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from .cmdb import CIType, make_ci_id
from .discovery_base import DISCOVERED_TAG, CommandRunner, DiscoveredCI
from .discovery_systemd import _FALLBACK_EPHEMERAL, _PROC_RE, _SS_RE

logger = logging.getLogger("skcoord.discovery")

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
                        (
                            "memory_total_bytes",
                            "memory_used_bytes",
                            "memory_free_bytes",
                            "memory_shared_bytes",
                            "memory_cache_bytes",
                            "memory_available_bytes",
                        ),
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
                    {
                        "source": parts[0],
                        "size_bytes": int(parts[1]),
                        "used_bytes": int(parts[2]),
                        "available_bytes": int(parts[3]),
                        "used_percent": parts[4],
                        "mountpoint": parts[5],
                    }
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


def _cron_rows(text: str, *, system: bool) -> list[tuple[str, str, str]]:
    """Return schedule, principal and command-name triples without secret arguments."""
    rows: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ("=" in line and not line.startswith("@")):
            continue
        parts = line.split()
        if line.startswith("@"):
            minimum = 3 if system else 2
            if len(parts) < minimum:
                continue
            schedule = parts[0]
            principal = parts[1] if system else "current-user"
            command = parts[2] if system else parts[1]
        else:
            minimum = 7 if system else 6
            if len(parts) < minimum:
                continue
            schedule = " ".join(parts[:5])
            principal = parts[5] if system else "current-user"
            command = parts[6] if system else parts[5]
        rows.append((schedule, principal, Path(command).name[:120]))
    return rows


def collect_cron_jobs(runner: CommandRunner) -> list[DiscoveredCI]:
    """Observed user and system crontab entries with command arguments redacted."""
    sources = (
        ("user", runner.run(["crontab", "-l"]), False),
        ("system", runner.run(["cat", "/etc/crontab"]), True),
    )
    out: list[DiscoveredCI] = []
    for scope, text, system in sources:
        if not text:
            continue
        for schedule, principal, command_name in _cron_rows(text, system=system):
            fingerprint = hashlib.sha256(
                f"{scope}\0{schedule}\0{principal}\0{command_name}".encode()
            ).hexdigest()[:16]
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=f"{runner.host}:cron:{scope}:{fingerprint}",
                    source=f"cron:{scope}",
                    observed=True,
                    node=runner.host,
                    attributes={
                        "schedule": schedule,
                        "principal": principal,
                        "command_name": command_name,
                        "command_arguments_redacted": True,
                    },
                    tags=("cronjob", "scheduler", DISCOVERED_TAG),
                    relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
                )
            )
    return out


def collect_network_interfaces(runner: CommandRunner) -> list[DiscoveredCI]:
    """Network CIs for non-loopback interfaces and their observed identities."""
    stdout = runner.run(["ip", "-j", "address", "show"])
    if not stdout:
        return []
    try:
        interfaces = json.loads(stdout)
    except (TypeError, ValueError):
        return []
    out: list[DiscoveredCI] = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        if not isinstance(interface, dict):
            continue
        name = str(interface.get("ifname", "")).strip()
        if not name or name == "lo":
            continue
        addresses = sorted(
            {
                str(info.get("local"))
                for info in interface.get("addr_info", [])
                if isinstance(info, dict)
                and info.get("local")
                and info.get("scope") in ("global", "site")
            }
        )
        attributes: dict[str, Any] = {
            "interface": name,
            "operstate": interface.get("operstate", "unknown"),
            "addresses": addresses,
        }
        if interface.get("address"):
            attributes["mac_address"] = interface["address"]
        out.append(
            DiscoveredCI(
                ci_type=CIType.NETWORK.value,
                name=f"{runner.host}:{name}",
                source="ip-address",
                observed=True,
                node=runner.host,
                attributes=attributes,
                tags=("network-interface", DISCOVERED_TAG),
                relationships=(("runs_on", make_ci_id(CIType.HOST.value, runner.host)),),
            )
        )
    return out


_PSEUDO_FILESYSTEMS = {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "proc",
    "pstore",
    "securityfs",
    "sysfs",
    "tracefs",
}

_PERSISTENT_FILESYSTEMS = {
    "9p",
    "btrfs",
    "ceph",
    "cifs",
    "drvfs",
    "ext2",
    "ext3",
    "ext4",
    "fuseblk",
    "nfs",
    "nfs4",
    "ntfs",
    "ntfs3",
    "smb3",
    "vfat",
    "xfs",
    "zfs",
}
