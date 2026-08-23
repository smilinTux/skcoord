"""Bounded, secret-free Syncthing health discovery.

The collector asks each target to query its own loopback REST endpoint. The
remote helper reads the API credential locally and never prints it, a folder
path, or file metadata. The returned service CI is host-qualified so one
fleet pass cannot fold ten same-named units into one ambiguous record.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .cmdb import CIStatus, CIType, make_ci_id
from .discovery_base import DISCOVERED_TAG, CommandRunner, DiscoveredCI

_SCHEMA_VERSION = 1
_MAX_FOLDERS = 32
_MAX_PAYLOAD_BYTES = 256 * 1024
_FOLDER_ID = re.compile(r"^[A-Za-z0-9_. -]{1,128}$")
_HEALTH_STATES = frozenset({"healthy", "syncing", "degraded", "down"})

_REMOTE_PROBE = r"""
import json
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_FOLDERS = 32
paths = (
    Path.home() / ".local/state/syncthing/config.xml",
    Path.home() / ".config/syncthing/config.xml",
)
config = next((path for path in paths if path.is_file()), None)
if config is None:
    print(json.dumps({"schema": 1, "configured": False}, separators=(",", ":")))
    raise SystemExit(0)

root = ET.parse(config).getroot()
folders = []
for element in root.iter("folder"):
    folder_id = (element.get("id") or "").strip()
    if not folder_id:
        continue
    folders.append(
        {
            "id": folder_id[:128],
            "paused": element.findtext("paused", "false") == "true",
        }
    )
folders = folders[:MAX_FOLDERS]
payload = {
    "schema": 1,
    "configured": True,
    "config_schema": int(root.get("version") or 0),
    "available": False,
    "folders": folders,
    "ports": [],
}

gui = root.find("gui")
key = gui.findtext("apikey", "") if gui is not None else ""
address = gui.findtext("address", "127.0.0.1:8384") if gui is not None else "127.0.0.1:8384"
try:
    port = int(address.rsplit(":", 1)[-1])
except ValueError:
    port = 8384
headers = {"X-API-Key": key}

def get(path, query=None):
    suffix = "" if not query else "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}{suffix}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError("response_too_large")
    return json.loads(data)

try:
    version = get("/rest/system/version")
    errors = get("/rest/system/error")
    connections = get("/rest/system/connections").get("connections") or {}
    payload.update(
        available=True,
        version=str(version.get("version") or "")[:64],
        system_errors=len(errors.get("errors") or []),
        connected_devices=sum(
            1 for value in connections.values() if isinstance(value, dict) and value.get("connected")
        ),
    )
    for folder in folders:
        if folder["paused"]:
            folder.update(state="paused", pending_items=0, pull_errors=0)
            continue
        try:
            status = get("/rest/db/status", {"folder": folder["id"]})
            pull_errors = status.get("pullErrors") or []
            if isinstance(pull_errors, list):
                pull_errors = len(pull_errors)
            folder.update(
                state=str(status.get("state") or "unknown")[:32],
                pending_items=max(0, int(status.get("needTotalItems") or 0)),
                pull_errors=max(0, int(pull_errors)),
            )
        except Exception:
            folder.update(state="unavailable", pending_items=0, pull_errors=0)
except Exception:
    payload["probe_status"] = "unavailable"

for candidate in (port, 22000):
    try:
        with socket.create_connection(("127.0.0.1", candidate), timeout=0.5):
            payload["ports"].append(candidate)
    except OSError:
        pass

print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
"""


def _bounded_int(value: object, *, maximum: int = 2**31 - 1) -> int:
    """Return a non-negative bounded integer from untrusted probe output."""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(number, 0), maximum)


def _folder_records(value: object) -> list[dict[str, Any]]:
    """Validate and bound the secret-free folder summary."""
    if not isinstance(value, list) or len(value) > _MAX_FOLDERS:
        raise ValueError("Syncthing folder evidence exceeds its bound")
    records = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Syncthing folder evidence must be objects")
        folder_id = str(raw.get("id") or "")
        if not _FOLDER_ID.fullmatch(folder_id):
            raise ValueError("Syncthing folder id is invalid")
        state = str(raw.get("state") or "unknown")[:32]
        records.append(
            {
                "folder_id": folder_id,
                "state": state,
                "paused": bool(raw.get("paused")),
                "pending_items": _bounded_int(raw.get("pending_items")),
                "pull_errors": _bounded_int(raw.get("pull_errors")),
            }
        )
    return records


def _health(available: bool, folders: list[dict[str, Any]], system_errors: int) -> str:
    """Derive one stable health state from the bounded folder evidence."""
    if not available:
        return "down"
    if system_errors or any(
        item["pull_errors"] or item["state"] in {"error", "unavailable"}
        for item in folders
    ):
        return "degraded"
    if any(
        item["pending_items"] or item["state"] not in {"idle", "paused"}
        for item in folders
    ):
        return "syncing"
    return "healthy"


def _port_ci(host: str, port: int) -> DiscoveredCI:
    """Return one locally confirmed Syncthing listener CI."""
    return DiscoveredCI(
        ci_type=CIType.PORT.value,
        name=f"{host}:{port}",
        source="syncthing:local-status",
        observed=True,
        node=host,
        attributes={
            "port": port,
            "proto": "tcp",
            "probe": "local-connect",
            "service_hint": "syncthing",
        },
        tags=("syncthing", "local-probe", DISCOVERED_TAG),
        relationships=(("runs_on", make_ci_id(CIType.HOST.value, host)),),
    )


def collect_syncthing_health(runner: CommandRunner) -> list[DiscoveredCI]:
    """Collect bounded folder health without exporting a credential or path."""
    stdout = runner.run(["python3", "-c", _REMOTE_PROBE])
    if stdout is None:
        return []
    if not stdout.strip():
        return []
    if len(stdout.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Syncthing health payload exceeds its bound")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Syncthing health payload is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA_VERSION:
        raise ValueError("unsupported Syncthing health payload schema")
    if not payload.get("configured"):
        return []

    folders = _folder_records(payload.get("folders"))
    available = bool(payload.get("available"))
    system_errors = _bounded_int(payload.get("system_errors"))
    health = _health(available, folders, system_errors)
    if health not in _HEALTH_STATES:  # pragma: no cover - defensive invariant
        raise ValueError("invalid Syncthing health state")
    pending = sum(item["pending_items"] for item in folders)
    pull_errors = sum(item["pull_errors"] for item in folders)
    service_name = f"syncthing@{runner.host}"
    raw_ports = payload.get("ports") or []
    if not isinstance(raw_ports, list):
        raise ValueError("Syncthing port evidence must be a list")
    port_numbers = sorted(
        {
            _bounded_int(port, maximum=65535)
            for port in raw_ports
            if _bounded_int(port, maximum=65535) in {8384, 22000}
        }
    )
    port_cis = [_port_ci(runner.host, port) for port in port_numbers]
    relationships = [("runs_on", make_ci_id(CIType.HOST.value, runner.host))]
    relationships.extend(("connects_to", item.ci_id) for item in port_cis)
    service = DiscoveredCI(
        ci_type=CIType.SERVICE.value,
        name=service_name,
        canonical_name=service_name,
        source="syncthing:local-status",
        observed=True,
        node=runner.host,
        description=f"Syncthing health on {runner.host}",
        attributes={
            "sync_available": available,
            "sync_health_state": health,
            "sync_version": str(payload.get("version") or "")[:64],
            "sync_config_schema": _bounded_int(
                payload.get("config_schema"), maximum=10000
            ),
            "sync_folder_count": len(folders),
            "sync_folders": folders,
            "sync_pending_items": min(pending, 2**31 - 1),
            "sync_pull_errors": min(pull_errors, 2**31 - 1),
            "sync_system_errors": system_errors,
            "sync_connected_devices": _bounded_int(
                payload.get("connected_devices"), maximum=10000
            ),
        },
        tags=("syncthing", "sync-health", DISCOVERED_TAG),
        relationships=tuple(relationships),
    )
    return [service, *port_cis]


def syncthing_ci_status(item: DiscoveredCI) -> str | None:
    """Translate Syncthing application health to a CMDB service status."""
    if item.ci_type != CIType.SERVICE.value or "sync-health" not in item.tags:
        return None
    state = item.attributes.get("sync_health_state")
    if state == "down":
        return CIStatus.DOWN.value
    if state == "degraded":
        return CIStatus.DEGRADED.value
    if state in {"healthy", "syncing"}:
        return CIStatus.OPERATIONAL.value
    return None
