from __future__ import annotations

from pathlib import Path
from threading import Lock

import pytest

from skcoord.cmdb import CIType, make_ci_id
from skcoord.infrastructure_discovery import (
    ProbeResult,
    ProxmoxTransportPolicy,
    SecureSSHRunner,
    SKVaultCredentialResolver,
    collect_network_fingerprints,
    collect_proxmox_inventory,
    normalize_host,
)


class FixtureProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []
        self.lock = Lock()

    def probe(self, host: str, port: int, timeout: float):
        with self.lock:
            self.calls.append((host, port, timeout))
        if (host, port) == ("192.0.2.1", 22):
            return ProbeResult(banner=b"SSH-2.0-fixture-secret-looking-banner", service="ssh\nspoof")
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
    assert ssh.observed and ssh.attributes["service_hint"] == "sshspoof"
    assert ssh.attributes["banner_bytes"] == len(b"SSH-2.0-fixture-secret-looking-banner")
    assert ssh.attributes["banner_bytes_hashed"] == ssh.attributes["banner_bytes"]
    assert not ssh.attributes["banner_truncated"]
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
        self.policy = ProxmoxTransportPolicy(timeout_seconds=5, max_rows=100)
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


def test_proxmox_collector_requires_policy_and_bounds_responses() -> None:
    api = FixtureProxmox()
    api.policy = ProxmoxTransportPolicy(max_rows=1)
    api.data["/nodes"] = {"data": [{"node": "pve1"}, {"node": "pve2"}]}
    with pytest.raises(ValueError, match="max_rows"):
        collect_proxmox_inventory(api)
    with pytest.raises(ValueError, match="verify TLS"):
        ProxmoxTransportPolicy(tls_verified=False)


@pytest.mark.parametrize("value", ["-oProxyCommand=evil", "bad host", "host\nname", "a..b"])
def test_normalize_host_rejects_ambiguous_or_option_values(value: str) -> None:
    with pytest.raises(ValueError, match="host"):
        normalize_host(value)


def test_normalize_host_canonicalizes_names_and_addresses() -> None:
    assert normalize_host("PVE1.Internal.") == "pve1.internal"
    assert normalize_host("2001:0db8::1") == "2001:db8::1"


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
    known_hosts.chmod(0o600)
    vault = FixtureVault(
        {
            "username": "collector",
            "identity_file": str(identity),
            "known_hosts_file": str(known_hosts),
            "hostname": "192.0.2.10",
            "port": 2222,
        }
    )
    credential = SKVaultCredentialResolver(vault).resolve("skvault://cmdb/pve")
    command = SecureSSHRunner("pve1.internal", credential).command(["uname", "-sr"])

    assert vault.references == ["skvault://cmdb/pve"]
    assert "StrictHostKeyChecking=yes" in command
    assert f"UserKnownHostsFile={known_hosts}" in command
    assert "BatchMode=yes" in command and "IdentitiesOnly=yes" in command
    assert "collector@192.0.2.10" in command
    assert credential.hostname == "192.0.2.10"
    assert command[command.index("-p") + 1] == "2222"
    assert "skvault://" not in " ".join(command)
    assert repr(credential) == "SSHCredential(<redacted>)"


@pytest.mark.parametrize("port", [0, 65536, "not-a-port"])
def test_vault_resolver_rejects_invalid_ssh_port(tmp_path: Path, port) -> None:
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.touch(mode=0o600)
    known_hosts.touch(mode=0o600)
    record = {
        "username": "collector",
        "identity_file": str(identity),
        "known_hosts_file": str(known_hosts),
        "port": port,
    }

    with pytest.raises(ValueError, match="port"):
        SKVaultCredentialResolver(FixtureVault(record)).resolve("skvault://ssh/node")


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
    known_hosts.chmod(0o600)
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


def test_vault_resolver_rejects_symlink_and_writable_known_hosts(tmp_path: Path) -> None:
    identity = tmp_path / "id"
    identity.touch(mode=0o600)
    real_known_hosts = tmp_path / "known_hosts.real"
    real_known_hosts.touch(mode=0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.symlink_to(real_known_hosts)
    record = {
        "username": "root",
        "identity_file": str(identity),
        "known_hosts_file": str(known_hosts),
    }
    with pytest.raises(ValueError, match="non-symlink"):
        SKVaultCredentialResolver(FixtureVault(record)).resolve("skvault://cmdb/test")

    known_hosts.unlink()
    known_hosts.touch(mode=0o666)
    # Path.touch is filtered by the runner's umask (GitHub-hosted runners use
    # 0022), so request mode alone may create 0644 and fail to exercise the
    # writable-file refusal. Set the unsafe mode explicitly.
    known_hosts.chmod(0o666)
    with pytest.raises(ValueError, match="writable"):
        SKVaultCredentialResolver(FixtureVault(record)).resolve("skvault://cmdb/test")


def test_banner_metadata_records_truncation_without_raw_content() -> None:
    class LargeBanner:
        def probe(self, host, port, timeout):
            return ProbeResult(banner=b"x" * 5000, service="http\x00injected")

    ci = collect_network_fingerprints(["192.0.2.1/32"], [80], LargeBanner())[0]
    assert ci.attributes["banner_bytes"] == 5000
    assert ci.attributes["banner_bytes_hashed"] == 4096
    assert ci.attributes["banner_truncated"] is True
    assert ci.attributes["service_hint"] == "httpinjected"
    assert "banner" not in ci.attributes
