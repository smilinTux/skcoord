"""Declared-state discovery collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Sequence

from .cmdb import CIType
from .discovery_base import (
    AUTHORITY_OBSERVED,
    DISCOVERED_TAG,
    DiscoveredCI,
)


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
                    str(v)
                    for v in (
                        obj.get("name"),
                        address.get("ip"),
                        address.get("ssh_alias"),
                    )
                    if v
                ),
            )
        )

    for kind, tag in (
        ("service", "service"),
        ("cronjob", "cronjob"),
        ("operatorapp", "app"),
    ):
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
                    # Preserved through the service/cronjob/operatorapp fold so
                    # drift() can tell an Operatorapp (a CLI tool, invoked on
                    # demand -- not a daemon) from a Service that is actually
                    # expected to have a running unit, timer or container.
                    "fleet_kind": obj.get("kind"),
                    "cli": spec.get("cli"),
                    "unit": spec.get("unit"),
                }.items()
                if v is not None
            }
            # spec.unit names the systemd unit this Service actually runs as
            # (e.g. skgateway-claude-wrapper -> claude-code-api.service). It
            # was recorded but never read by anything that builds the alias
            # set drift() matches against -- dead data that let a spec and its
            # own declared running unit look like two unrelated things.
            aliases = tuple(str(v) for v in (spec.get("unit"),) if v)
            out.append(
                DiscoveredCI(
                    ci_type=CIType.SERVICE.value,
                    name=name,
                    source=f"fleet:{kind}",
                    description=(spec.get("note") or "")[:200],
                    attributes=attributes,
                    tags=("fleet", tag, DISCOVERED_TAG),
                    aliases=aliases,
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
        pid_file = obj.get("pid_file")
        attributes = {
            k: v
            for k, v in {
                "health_url": obj.get("health_url"),
                "registered_at": obj.get("registered_at"),
                "registered_by": obj.get("registered_by"),
                "pid_file": pid_file,
            }.items()
            if v is not None
        }
        # The pid file's stem is sometimes the only other name this service is
        # known by (e.g. a systemd unit built around the same basename); when
        # it differs from the registered name it is worth trying as an alias
        # too, the same way spec.unit is for a fleet:service object.
        aliases: tuple[str, ...] = ()
        if pid_file:
            stem = Path(str(pid_file)).stem
            if stem and stem.lower() != name.lower():
                aliases = (stem,)
        out.append(
            DiscoveredCI(
                ci_type=CIType.SERVICE.value,
                name=name,
                source="registry",
                attributes=attributes,
                tags=("registry", DISCOVERED_TAG),
                aliases=aliases,
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
