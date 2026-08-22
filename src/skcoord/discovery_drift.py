"""Drift findings derived from declared, observed, and stored state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .cmdb import CIStatus, CIType, CMDBManager
from .discovery_base import DISCOVERED_TAG, DiscoveredCI
from .discovery_scan import _matching_identity_ids
from .discovery_systemd import ORIGIN_DISTRO, ORIGIN_UNKNOWN


@dataclass
class DriftFinding:
    ci_id: str
    kind: str
    detail: str

    def as_dict(self) -> dict:
        return {"ci_id": self.ci_id, "kind": self.kind, "detail": self.detail}


def drift(
    discovered: Sequence[DiscoveredCI], mgr: Optional[CMDBManager] = None
) -> list[DriftFinding]:
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
        stored = mgr.list_cis()
        seen = {ci_id for item in discovered for ci_id in _matching_identity_ids(item, stored)}
        for ci in stored:
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
