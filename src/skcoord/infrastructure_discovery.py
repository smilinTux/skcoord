"""Bounded, transport-injected infrastructure discovery collectors.

The collectors in this module deliberately do not implement network or vault
I/O.  Operators inject a transport, which keeps policy at the call site and
makes every collector fixture-testable without touching a live system.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import quote

from .cmdb import CIType, make_ci_id
from .discovery import DISCOVERED_TAG, DiscoveredCI, _exec


@dataclass(frozen=True)
class ProbeResult:
    """Credentialless TCP observation returned by a caller-owned transport."""

    banner: bytes = b""
    service: str = ""


class ProbeTransport(Protocol):
    def probe(self, host: str, port: int, timeout: float) -> Optional[ProbeResult]: ...


def normalize_host(value: str) -> str:
    """Return a canonical IP/hostname, rejecting SSH options and ambiguous input."""
    host = value.strip()
    if not host or host.startswith("-") or any(ord(char) < 33 or ord(char) == 127 for char in host):
        raise ValueError("invalid host")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        host = host.rstrip(".").lower()
        labels = host.split(".")
        if not labels or len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("invalid host") from None
        return host


def _network_targets(networks: Sequence[str], max_hosts: int) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for text in networks:
        network = ipaddress.ip_network(text, strict=False)
        for address in network.hosts():
            value = str(address)
            if value not in seen:
                seen.add(value)
                targets.append(value)
            if len(targets) > max_hosts:
                raise ValueError(f"network scope exceeds max_hosts={max_hosts}")
    return targets


def collect_network_fingerprints(
    networks: Sequence[str],
    ports: Sequence[int],
    transport: ProbeTransport,
    *,
    max_hosts: int = 256,
    max_ports: int = 32,
    workers: int = 16,
    timeout: float = 1.0,
) -> list[DiscoveredCI]:
    """Collect open TCP endpoints with explicit limits and no credentials.

    Banners are reduced to length and SHA-256; raw remote content is never put
    in the CMDB.  Invalid or excessive scope fails closed before any probe.
    """
    if max_hosts < 1 or max_ports < 1 or not 1 <= workers <= 64 or timeout <= 0:
        raise ValueError("discovery limits must be positive (workers <= 64)")
    targets = _network_targets(networks, max_hosts)
    unique_ports = sorted(set(ports))
    if len(unique_ports) > max_ports or any(not 1 <= port <= 65535 for port in unique_ports):
        raise ValueError("port scope is invalid or exceeds max_ports")

    observations: list[tuple[str, int, ProbeResult]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cmdb-probe") as pool:
        futures = {
            pool.submit(transport.probe, host, port, timeout): (host, port)
            for host in targets
            for port in unique_ports
        }
        for future in as_completed(futures):
            host, port = futures[future]
            try:
                result = future.result()
            except Exception:  # a failed endpoint cannot blind the scan
                continue
            if result is not None:
                observations.append((host, port, result))

    out: list[DiscoveredCI] = []
    for host, port, result in sorted(observations, key=lambda row: (row[0], row[1])):
        banner = result.banner[:4096]
        attributes: dict[str, Any] = {"port": port, "proto": "tcp", "probe": "connect"}
        if result.service:
            service = "".join(char for char in result.service if char.isprintable()).strip()
            if service:
                attributes["service_hint"] = service[:80]
        if banner:
            attributes.update(
                banner_bytes=len(result.banner),
                banner_bytes_hashed=len(banner),
                banner_truncated=len(result.banner) > len(banner),
                banner_sha256=hashlib.sha256(banner).hexdigest(),
            )
        out.append(
            DiscoveredCI(
                ci_type=CIType.PORT.value,
                name=f"{host}:{port}",
                source="network:fingerprint",
                observed=True,
                node=host,
                attributes=attributes,
                tags=("network", "credentialless", DISCOVERED_TAG),
                relationships=(("runs_on", make_ci_id(CIType.HOST.value, host)),),
            )
        )
    return out


class ProxmoxAPITransport(Protocol):
    """Authenticated API transport supplied by the operator."""

    def get(self, path: str) -> Any: ...

    @property
    def policy(self) -> "ProxmoxTransportPolicy": ...


@dataclass(frozen=True)
class ProxmoxTransportPolicy:
    """Security properties the injected Proxmox adapter must enforce."""

    tls_verified: bool = True
    timeout_seconds: float = 10.0
    max_rows: int = 10_000

    def __post_init__(self) -> None:
        if not self.tls_verified:
            raise ValueError("Proxmox transport must verify TLS")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("Proxmox timeout must be between 0 and 60 seconds")
        if not 1 <= self.max_rows <= 100_000:
            raise ValueError("Proxmox max_rows must be between 1 and 100000")


def _rows(value: Any, max_rows: int) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get("data", [])
    if not isinstance(value, list):
        return []
    if len(value) > max_rows:
        raise ValueError(f"Proxmox response exceeds max_rows={max_rows}")
    return [row for row in value if isinstance(row, Mapping)]


def collect_proxmox_inventory(transport: ProxmoxAPITransport) -> list[DiscoveredCI]:
    """Collect Proxmox nodes, guests, and storage via an injected API client."""
    policy = transport.policy
    # Accessing and validating a concrete policy makes TLS, timeout, and response
    # bounds part of the adapter contract rather than undocumented assumptions.
    if not isinstance(policy, ProxmoxTransportPolicy):
        raise TypeError("Proxmox transport must expose ProxmoxTransportPolicy")
    out: list[DiscoveredCI] = []
    nodes = sorted(
        _rows(transport.get("/nodes"), policy.max_rows), key=lambda row: str(row.get("node", ""))
    )
    for node in nodes:
        raw_name = str(node.get("node", "")).strip()
        if not raw_name:
            continue
        name = normalize_host(raw_name)
        if not name:
            continue
        node_id = make_ci_id(CIType.HOST.value, name)
        node_path = quote(name, safe="")
        out.append(
            DiscoveredCI(
                ci_type=CIType.HOST.value,
                name=name,
                source="proxmox:node",
                observed=True,
                node=name,
                attributes={
                    k: node[k] for k in ("status", "cpu", "maxcpu", "mem", "maxmem") if k in node
                },
                tags=("proxmox", "hypervisor", DISCOVERED_TAG),
            )
        )
        for kind, endpoint in (("qemu", "qemu"), ("lxc", "lxc")):
            guests = _rows(transport.get(f"/nodes/{node_path}/{endpoint}"), policy.max_rows)
            for guest in sorted(guests, key=lambda row: str(row.get("vmid", ""))):
                vmid = guest.get("vmid")
                if vmid is None:
                    continue
                guest_name = str(guest.get("name") or f"{name}-{kind}-{vmid}")
                attrs = {
                    k: guest[k]
                    for k in ("vmid", "status", "template", "cpus", "maxmem")
                    if k in guest
                }
                attrs["virtualization"] = kind
                out.append(
                    DiscoveredCI(
                        ci_type=CIType.HOST.value,
                        name=guest_name,
                        source=f"proxmox:{kind}",
                        observed=True,
                        node=name,
                        attributes=attrs,
                        tags=("proxmox", kind, DISCOVERED_TAG),
                        relationships=(("runs_on", node_id),),
                    )
                )
        for storage in _rows(transport.get(f"/nodes/{node_path}/storage"), policy.max_rows):
            storage_name = str(storage.get("storage", "")).strip()
            if storage_name:
                out.append(
                    DiscoveredCI(
                        ci_type=CIType.DATASTORE.value,
                        name=f"{name}:{storage_name}",
                        source="proxmox:storage",
                        observed=True,
                        node=name,
                        attributes={
                            k: storage[k]
                            for k in ("storage", "type", "active", "total", "used")
                            if k in storage
                        },
                        tags=("proxmox", "storage", DISCOVERED_TAG),
                        relationships=(("runs_on", node_id),),
                    )
                )
    return out


class VaultTransport(Protocol):
    """Minimal skvault adapter; implementations must return metadata only."""

    def resolve_ssh(self, reference: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, repr=False)
class SSHCredential:
    username: str
    identity_file: Path
    known_hosts_file: Path

    def __repr__(self) -> str:
        return "SSHCredential(<redacted>)"


_SAFE_USER = re.compile(r"^[a-zA-Z0-9_.-]+$")


class SKVaultCredentialResolver:
    """Resolve an opaque skvault reference into safe SSH file metadata."""

    def __init__(self, transport: VaultTransport) -> None:
        self._transport = transport

    def resolve(self, reference: str) -> SSHCredential:
        if not reference.startswith("skvault://"):
            raise ValueError("SSH credentials require an skvault:// reference")
        record = self._transport.resolve_ssh(reference)
        if "password" in record or "private_key" in record:
            raise ValueError("inline SSH secrets are forbidden; use protected files")
        username = str(record.get("username", ""))
        identity = Path(str(record.get("identity_file", ""))).expanduser()
        known_hosts = Path(str(record.get("known_hosts_file", ""))).expanduser()
        if not _SAFE_USER.fullmatch(username):
            raise ValueError("invalid SSH username")
        if not identity.is_absolute() or not known_hosts.is_absolute():
            raise ValueError("SSH credential paths must be absolute")
        for path, label in ((identity, "identity"), (known_hosts, "known_hosts")):
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"SSH {label} must be an existing regular non-symlink file")
            stat = path.stat()
            if stat.st_uid != os.geteuid():
                raise ValueError(f"SSH {label} must be owned by the current user")
            if stat.st_mode & 0o022:
                raise ValueError(f"SSH {label} must not be group/world writable")
        if identity.stat().st_mode & 0o077:
            raise ValueError("SSH identity file must not be group/world accessible")
        return SSHCredential(username, identity, known_hosts)


@dataclass
class SecureSSHRunner:
    """SSH command runner with mandatory known-host pinning and batch mode."""

    host: str
    credential: SSHCredential
    timeout: int = 20

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("SSH timeout must be positive")
        self.host = normalize_host(self.host)

    def command(self, argv: Sequence[str]) -> list[str]:
        remote = " ".join(shlex.quote(arg) for arg in argv)
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.credential.known_hosts_file}",
            "-o",
            f"ConnectTimeout={min(self.timeout, 10)}",
            "-i",
            str(self.credential.identity_file),
            f"{self.credential.username}@{self.host}",
            remote,
        ]

    def run(self, argv: Sequence[str]) -> Optional[str]:
        return _exec(self.command(argv), self.timeout)
