from __future__ import annotations

from pathlib import Path
from threading import Lock

import pytest

from skcoord.cmdb import CIType, make_ci_id
from skcoord.infrastructure_discovery import (
    ProbeResult,
    SecureSSHRunner,
    SKVaultCredentialResolver,
    collect_network_fingerprints,
    collect_proxmox_inventory,
)


class FixtureProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.lock = Lock()

    def probe(self, host: str, port: int, timeout: float):
        with self.lock:
            self.calls.append((host, port, timeout))
        if (host, port) == ("192.0.2.1", 22):
            return ProbeResult(banner=b"SSH-2.0-fixture-secret-looking-banner", service="ssh")
        if port == 443:
            return ProbeResult(service="tls")
        return None


def test_network_collector_is_bounded_deterministic_and_drops_raw_banners() -> None:
    transport = FixtureProbe()
    found = collect_network_fingerprints(
        ["192.0.2.0/30"], [443, 22, 443], transport, workers=2, timeout=0.25
    )

    assert [ci.name for ci in found] == ["192.0.2.1:22", "192.0.2.1:443", "192.0.2.2:443"]
    assert len(transport.calls) == 4
    ssh = found[0]
    assert ssh.observed and ssh.attributes["service_hint"] == "ssh"
    assert ssh.attributes["banner_bytes"] == len(b"SSH-2.0-fixture-secret-looking-banner")
    assert "banner" not in ssh.attributes
    assert "secret-looking" not in repr(ssh)


def test_network_collector_rejects_excessive_scope_before_probing() -> None:
    transport = FixtureProbe()
    with pytest.raises(ValueError, match="max_hosts"):
        collect_network_fingerprints(["10.0.0.0/24"], [22], transport, max_hosts=8)
    assert transport.calls == []


def test_network_collector_rejects_bad_limits_and_ports() -> None:
    transport = FixtureProbe()
    with pytest.raises(ValueError):
        collect_network_fingerprints(["192.0.2.1/32"], [0], transport)
    with pytest.raises(ValueError):
        collect_network_fingerprints(["192.0.2.1/32"], range(1, 40), transport, max_ports=4)
    assert transport.calls == []


class FixtureProxmox:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.data = {
            "/nodes": {"data": [{"node": "pve1", "status": "online", "maxcpu": 16}]},
            "/nodes/pve1/qemu": {"data": [{"vmid": 101, "name": "api-vm", "status": "running"}]},
            "/nodes/pve1/lxc": {"data": [{"vmid": 202, "name": "worker-ct", "status": "stopped"}]},
            "/nodes/pve1/storage": {
                "data": [{"storage": "local-zfs", "type": "zfspool", "active": 1}]
            },
        }

    def get(self, path: str):
        self.calls.append(path)
        return self.data[path]


def test_proxmox_collector_uses_injected_transport_and_builds_relationships() -> None:
    api = FixtureProxmox()
    found = collect_proxmox_inventory(api)

    assert api.calls == ["/nodes", "/nodes/pve1/qemu", "/nodes/pve1/lxc", "/nodes/pve1/storage"]
    assert {(ci.ci_type, ci.name) for ci in found} == {
        (CIType.HOST.value, "pve1"),
        (CIType.HOST.value, "api-vm"),
        (CIType.HOST.value, "worker-ct"),
        (CIType.DATASTORE.value, "pve1:local-zfs"),
    }
    parent = make_ci_id(CIType.HOST.value, "pve1")
    assert all(("runs_on", parent) in ci.relationships for ci in found if ci.name != "pve1")
    assert next(ci for ci in found if ci.name == "api-vm").attributes["virtualization"] == "qemu"


class FixtureVault:
    def __init__(self, record):
        self.record = record
        self.references: list[str] = []

    def resolve_ssh(self, reference: str):
        self.references.append(reference)
        return self.record


def test_vault_resolver_and_ssh_runner_enforce_strict_host_keys(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("fixture key path; not a real key")
    identity.chmod(0o600)
    known_hosts.write_text("pve1 fixture-host-key")
    vault = FixtureVault(
        {
            "username": "collector",
            "identity_file": str(identity),
            "known_hosts_file": str(known_hosts),
        }
    )
    credential = SKVaultCredentialResolver(vault).resolve("skvault://cmdb/pve")
    command = SecureSSHRunner("pve1.internal", credential).command(["uname", "-sr"])

    assert vault.references == ["skvault://cmdb/pve"]
    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={known_hosts}" in command
    assert "BatchMode=yes" in command and "IdentitiesOnly=yes" in command
    assert "collector@pve1.internal" in command
    assert "skvault://" not in " ".join(command)
    assert repr(credential) == "SSHCredential(<redacted>)"


@pytest.mark.parametrize(
    "record",
    [
        {"username": "root", "private_key": "TOP SECRET", "known_hosts_file": "/x"},
        {
            "username": "root",
            "password": "TOP SECRET",
            "identity_file": "/x",
            "known_hosts_file": "/y",
        },
        {"username": "bad user", "identity_file": "/x", "known_hosts_file": "/y"},
        {"username": "root", "identity_file": "relative", "known_hosts_file": "/y"},
    ],
)
def test_vault_resolver_rejects_inline_secrets_and_unsafe_metadata(record) -> None:
    with pytest.raises(ValueError):
        SKVaultCredentialResolver(FixtureVault(record)).resolve("skvault://cmdb/test")


def test_vault_resolver_rejects_non_vault_references_without_calling_transport() -> None:
    vault = FixtureVault({})
    with pytest.raises(ValueError, match="skvault"):
        SKVaultCredentialResolver(vault).resolve("env://PASSWORD")
    assert vault.references == []


def test_ssh_runner_rejects_host_option_injection(tmp_path: Path) -> None:
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.touch(mode=0o600)
    known_hosts.touch()
    credential = SKVaultCredentialResolver(
        FixtureVault(
            {
                "username": "root",
                "identity_file": str(identity),
                "known_hosts_file": str(known_hosts),
            }
        )
    ).resolve("skvault://cmdb/test")
    with pytest.raises(ValueError, match="host"):
        SecureSSHRunner("-oProxyCommand=evil", credential)
