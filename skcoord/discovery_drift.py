"""Drift findings derived from declared, observed, and stored state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .cmdb import CIStatus, CIType, CMDBManager
from .discovery_base import DISCOVERED_TAG, DiscoveredCI
from .discovery_identity import identity_estate_drift
from .discovery_scan import _matching_identity_ids
from .discovery_systemd import ORIGIN_DISTRO, ORIGIN_UNKNOWN

OPERATORAPP_KIND = "Operatorapp"
"""``fleet/objects/operatorapp/*.json``'s ``kind`` -- a CLI-invoked tool
(``spec.cli``), not a daemon. It is folded into CIType.SERVICE like everything
else in ``collect_fleet_objects``, so this is how drift() tells it apart from
a Service that is genuinely expected to have a running unit."""


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
      Operatorapp-kind CIs (a CLI tool, not a daemon) are exempt: they have
      no running unit by design, and that is not a gap.
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
    findings.extend(DriftFinding(*finding) for finding in identity_estate_drift(list(discovered)))
    services = [d for d in discovered if d.ci_type == CIType.SERVICE.value]

    # Flags cover the merged case (one CI both declared and observed); the key
    # sets cover records that never merged because their names differ, e.g. a
    # spec saying "skgateway" against a unit called "skgateway.service". Every
    # identity alias is a candidate key, not just ``.name``: a cronjob observed
    # under a fingerprinted name but wrapped by sk-cron-run, or a fleet object
    # whose ``spec.unit``/registry ``pid_file`` names the real running unit,
    # would otherwise be structurally unmatchable no matter what is running.
    observed_keys: set[str] = set()
    declared_keys: set[str] = set()
    for d in services:
        keys = _service_keys(d)
        if d.observed:
            observed_keys |= keys
        if d.declared:
            declared_keys |= keys

    for item in services:
        if item.observed or not item.declared:
            continue
        if item.attributes.get("fleet_kind") == OPERATORAPP_KIND:
            # A CLI-invoked tool (spec.cli), not a daemon. It has no running
            # unit, timer or container by design -- that is not a gap.
            continue
        if not (_service_keys(item) & observed_keys):
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
        if not (_service_keys(item) & declared_keys):
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


def _service_keys(item: DiscoveredCI) -> set[str]:
    """All normalised keys a service CI can be recognised by.

    ``identity_aliases`` already folds in ``name``, ``canonical_name`` and any
    declared aliases (sk-cron-run job names, ``spec.unit``, registry
    ``pid_file`` stems); this just applies the same ``.service``/``.timer``
    and case/underscore normalisation ``_service_key`` gives the bare name.
    """
    return {_service_key(alias) for alias in item.identity_aliases}
