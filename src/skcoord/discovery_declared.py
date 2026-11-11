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


_SYSTEMCTL_START_VERBS = frozenset(
    {"start", "restart", "try-restart", "reload-or-restart", "try-reload-or-restart"}
)


def _command_unit_aliases(command: object) -> tuple[str, ...]:
    """Units a ``systemctl [--user] start <unit>...`` command names.

    A CronJob whose whole job is to start a unit (``skcounter-edge-<node>``:
    ``systemctl --user start skcounter-edge.service``) is that unit's timer
    run under a fleet name. Only this exact shape is read: guessing a unit
    from an arbitrary CLI (``skos brief send``) would invent matches.
    """
    if not isinstance(command, str):
        return ()
    parts = command.split()
    if not parts or Path(parts[0]).name != "systemctl":
        return ()
    words = [p for p in parts[1:] if not p.startswith("-")]
    if not words or words[0] not in _SYSTEMCTL_START_VERBS:
        return ()
    return tuple(w for w in words[1:] if "." in w or "@" in w)


def _node_index(root: Path) -> list[tuple[str, set[str], dict]]:
    """``(object name, names it is known by, labels)`` per fleet Node."""
    out: list[tuple[str, set[str], dict]] = []
    for path in sorted((root / "node").glob("*.json")):
        obj = _read_json(path)
        if not obj:
            continue
        name = str(obj.get("name") or path.stem)
        address = (obj.get("spec") or {}).get("address") or {}
        names = {name, name.removeprefix("node-")}
        names |= {str(v) for v in (address.get("hostname"), address.get("ip")) if v}
        labels = obj.get("labels") or {}
        out.append((name, {n.lower() for n in names}, labels if isinstance(labels, dict) else {}))
    return out


def _resolve_node(value: object, nodes: list[tuple[str, set[str], dict]]) -> Optional[str]:
    if not value:
        return None
    key = str(value).strip().lower()
    for name, names, _labels in nodes:
        if key in names:
            return name
    return str(value).strip()


def _target_nodes(
    fleet_root: Path,
    kind: str,
    name: str,
    spec: dict,
    nodes: list[tuple[str, set[str], dict]],
) -> Optional[list[str]]:
    """The fleet node(s) a Service/CronJob is meant to run on, or ``None``.

    Same precedence the fleet's own CronController uses for a CronJob's node:
    a placement record, else the node(s) reporting status for it. Failing
    both, the ``nodeSelector`` is evaluated against Node labels with the
    scheduler's exact-match AND semantics, so a selector no node satisfies
    yields ``[]`` (placeable nowhere). ``None`` means no evidence at all, and
    drift() then treats the declaration as it always has.
    """
    placement = _read_json(fleet_root / "placements" / kind / f"{name}.json")
    if placement and placement.get("node"):
        resolved = _resolve_node(placement["node"], nodes)
        return [resolved] if resolved else []
    reporting = set()
    for path in sorted((fleet_root / "status").glob(f"*/{kind}/{name}.json")):
        status = _read_json(path) or {}
        resolved = _resolve_node(status.get("node") or path.parent.parent.name, nodes)
        if resolved:
            reporting.add(resolved)
    if reporting:
        return sorted(reporting)
    selector = spec.get("nodeSelector")
    if isinstance(selector, dict) and selector and nodes:
        return sorted(
            node
            for node, _names, labels in nodes
            if all(labels.get(k) == v for k, v in selector.items())
        )
    return None


def collect_fleet_objects(home: Path) -> list[DiscoveredCI]:
    """Host / service CIs from the skfleet object model.

    ``~/.skcapstone/fleet/objects/{node,service,cronjob,operatorapp}`` is the
    fleet control plane's own declarative inventory, which makes it the closest
    thing to an authoritative asset list we already maintain.
    """
    out: list[DiscoveredCI] = []
    fleet_root = Path(home).expanduser() / "fleet"
    root = fleet_root / "objects"
    nodes = _node_index(root)

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
                    for v in (obj.get("name"), address.get("ip"), address.get("ssh_alias"))
                    if v
                ),
            )
        )

    for kind, tag in (("service", "service"), ("cronjob", "cronjob"), ("operatorapp", "app")):
        for path in sorted((root / kind).glob("*.json")):
            obj = _read_json(path)
            if not obj:
                continue
            spec = obj.get("spec") or {}
            if spec.get("deleted"):
                # The fleet's own tombstone: a retired object stays on disk
                # (generation history) but declares nothing. The Cron and
                # Service controllers already skip it; so must discovery.
                continue
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
            # A CronJob that only starts a unit is matched through that unit.
            aliases = tuple(
                dict.fromkeys(
                    str(v)
                    for v in (spec.get("unit"), *_command_unit_aliases(spec.get("command")))
                    if v
                )
            )
            # Which node(s) this declaration targets, so a scan of one node
            # does not count another node's per-node job as missing there.
            targets = _target_nodes(fleet_root, kind, name, spec, nodes)
            if targets is not None:
                attributes["target_nodes"] = targets
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
        # skcapstone's register_service records the systemd unit the caller
        # runs under (from its own cgroup), so ``capauth`` registered by
        # capauth-authz.service can be tied to that unit.
        unit = obj.get("systemd_unit")
        if isinstance(unit, str) and unit:
            aliases = (*aliases, unit)
            attributes["systemd_unit"] = unit
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
