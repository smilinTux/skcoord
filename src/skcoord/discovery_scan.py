"""Discovery collector registry, identity folding, and scan orchestration."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .cmdb import CIType
from .discovery_assets import (
    collect_datastores,
    collect_model_endpoints,
    collect_observed_agents,
)
from .discovery_base import (
    AUTHORITY_DECLARED,
    AUTHORITY_OBSERVED,
    CommandRunner,
    DiscoveredCI,
    _normalise_alias,
    _utc_now,
)
from .discovery_declared import collect_agents, collect_fleet_objects, collect_registry
from .discovery_identity import collect_identity_estate
from .discovery_runtime import (
    collect_cron_jobs,
    collect_host_facts,
    collect_listening_ports,
    collect_network_interfaces,
)
from .discovery_syncthing import collect_syncthing_health
from .discovery_systemd import collect_docker_containers, collect_systemd_units

logger = logging.getLogger("skcoord.discovery")

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
    collect_cron_jobs,
    collect_network_interfaces,
    collect_datastores,
    collect_observed_agents,
    collect_model_endpoints,
    collect_identity_estate,
    collect_syncthing_health,
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


def _matching_identity_ids(item: DiscoveredCI, existing: Iterable[object]) -> set[str]:
    """Resolve host/device aliases consistently for reconcile and drift."""
    if item.ci_type not in (CIType.HOST.value, CIType.DEVICE.value):
        return {item.ci_id}
    aliases = set(item.identity_aliases)
    matches = {item.ci_id}
    for ci in existing:
        if getattr(ci, "ci_type", "") not in (CIType.HOST.value, CIType.DEVICE.value):
            continue
        values = (
            getattr(ci, "id", "").removeprefix(f"ci-{getattr(ci, 'ci_type', '')}-"),
            getattr(ci, "name", ""),
            getattr(ci, "attributes", {}).get("canonical_name", ""),
            *getattr(ci, "attributes", {}).get("aliases", []),
        )
        if aliases & {_normalise_alias(value) for value in values if value}:
            matches.add(ci.id)
    return matches


def scan(
    home: Path,
    runners: Sequence[CommandRunner] = (),
    include_declared: bool = True,
) -> list[DiscoveredCI]:
    """Run every collector and fold the results.

    ``runners`` is empty by default so a scan is never implicitly a scan of
    *this* box: the caller says which machines to ask.

    The connects_to collectors (card 6e010c63) live in
    ``discovery_connects.py`` because this module is at its file-size
    budget (debt card 74ea00dd) and are imported here, inside the function:
    that module imports this one's model and runner types, so a top-level
    import would be circular.
    """
    from .discovery_connects import CONNECTS_COLLECTORS

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
        for observer in (*OBSERVED_COLLECTORS, *CONNECTS_COLLECTORS):
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
                logger.exception(
                    "observed collector failed: %s on %s",
                    observer.__name__,
                    runner.host,
                )
    return merge(found)


# ---------------------------------------------------------------------------
# Reconcile + drift
# ---------------------------------------------------------------------------
