"""CMDB reconciliation for discovered configuration items."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from .cmdb import (
    ALLOWED_RELATIONSHIPS,
    CIStatus,
    CIType,
    CMDBManager,
    is_secret_attribute_key,
)
from .discovery_base import (
    AUTHORITY_DECLARED,
    AUTHORITY_OBSERVED,
    DISCOVERED_TAG,
    DiscoveredCI,
)
from .discovery_scan import _matching_identity_ids, merge


@dataclass
class ReconcileReport:
    """What a reconcile did, or would do when ``applied`` is False."""

    applied: bool = False
    created: list[str] = field(default_factory=list)
    updated: dict[str, list[str]] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    relationships: list[dict[str, str]] = field(default_factory=list)
    validation_failures: list[dict[str, str]] = field(default_factory=list)
    secret_redaction_findings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "orphans": self.orphans,
            "relationships": self.relationships,
            "validation_failures": self.validation_failures,
            "secret_redaction_findings": self.secret_redaction_findings,
            "counts": {
                "created": len(self.created),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "orphans": len(self.orphans),
                "relationships": len(self.relationships),
                "validation_failures": len(self.validation_failures),
                "secret_redaction_findings": len(self.secret_redaction_findings),
            },
        }


def _redact_attributes(
    value: Any, *, ci_id: str, path: str = "attributes"
) -> tuple[Any, list[dict[str, str]]]:
    """Redact secret-bearing attribute keys and return non-secret findings."""
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        clean = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if is_secret_attribute_key(key):
                findings.append({"ci_id": ci_id, "path": child_path})
            else:
                clean[key], nested = _redact_attributes(
                    child, ci_id=ci_id, path=child_path
                )
                findings.extend(nested)
        return clean, findings
    if isinstance(value, (list, tuple)):
        clean_items = []
        for index, child in enumerate(value):
            clean, nested = _redact_attributes(
                child, ci_id=ci_id, path=f"{path}[{index}]"
            )
            clean_items.append(clean)
            findings.extend(nested)
        return clean_items, findings
    return value, findings


def _validated_discovery(
    discovered: Sequence[DiscoveredCI], mgr: CMDBManager
) -> tuple[list[DiscoveredCI], list[dict[str, str]], list[dict[str, str]]]:
    """Normalize one evidence batch and validate it before any event is appended."""
    normalized = merge(discovered)
    known_ids = {ci.id for ci in mgr.list_cis()} | {item.ci_id for item in normalized}
    allowed_types = {item.value for item in CIType}
    failures: list[dict[str, str]] = []
    redactions: list[dict[str, str]] = []
    clean_items: list[DiscoveredCI] = []
    for item in normalized:
        ci_id = item.ci_id
        if item.ci_type not in allowed_types:
            failures.append(
                {"ci_id": ci_id, "field": "ci_type", "reason": "unsupported"}
            )
        if not (item.canonical_name or item.name).strip():
            failures.append({"ci_id": ci_id, "field": "name", "reason": "empty"})
        if not isinstance(item.attributes, dict):
            failures.append(
                {"ci_id": ci_id, "field": "attributes", "reason": "must be an object"}
            )
            attributes = {}
        else:
            attributes, found = _redact_attributes(item.attributes, ci_id=ci_id)
            redactions.extend(found)
        for rel_type, target in item.relationships:
            if rel_type not in ALLOWED_RELATIONSHIPS:
                failures.append(
                    {
                        "ci_id": ci_id,
                        "field": "relationships",
                        "reason": f"unsupported relationship: {rel_type}",
                    }
                )
            elif target == ci_id:
                failures.append(
                    {"ci_id": ci_id, "field": "relationships", "reason": "self edge"}
                )
            elif target not in known_ids:
                failures.append(
                    {
                        "ci_id": ci_id,
                        "field": "relationships",
                        "reason": f"missing target: {target}",
                    }
                )
        clean_items.append(replace(item, attributes=attributes))
    return clean_items, failures, redactions


def _observed_status(item: DiscoveredCI) -> Optional[str]:
    """CIStatus a collector's observation implies, or None.

    Precedence rule (written down, CMDB-6): for discovery-owned CIs the
    observed systemd state IS the health. ``active_state=active`` reads
    operational, ``failed`` reads down. Every other systemd state (inactive,
    activating, ...) is ambiguous in service terms (a oneshot or timer is
    legitimately inactive), so we do not force a status for it and leave any
    existing value alone. ITIL incident severity is informational and is
    surfaced via ``impact_analysis().open_incidents``; it never overrides an
    observed state on the CIStatus field.
    """
    if not item.observed or item.ci_type != CIType.SERVICE.value:
        return None
    from .discovery_syncthing import syncthing_ci_status

    sync_status = syncthing_ci_status(item)
    if sync_status is not None:
        return sync_status
    state = item.attributes.get("active_state")
    if state == "active":
        return CIStatus.OPERATIONAL.value
    if state == "failed":
        return CIStatus.DOWN.value
    return None


def reconcile(
    mgr: CMDBManager,
    discovered: Sequence[DiscoveredCI],
    agent: str = "cmdb-discovery",
    apply: bool = False,
    scan_complete: bool = True,
) -> ReconcileReport:
    """Converge the CMDB on what was discovered. Additive only.

    Nothing is deleted, ever. A CI that discovery no longer sees is reported as
    an orphan and left alone: the store is event-sourced and shared, so a
    collector that silently failed must not be able to erase inventory.
    Callers must pass ``scan_complete=False`` when any required collector or
    target failed; absence from a partial scan then produces no orphan signal.

    Status precedence (CMDB-6): CIStatus on a discovery-owned CI is derived
    from the observed state, so a CI can never read ``degraded`` while its own
    ``active_state`` attribute says ``active``. An ITIL incident's severity is
    its own signal (see ``impact_analysis``); it does not get folded into the
    headline CIStatus. A manually retired CI is never un-retired by reconcile.
    """
    report = ReconcileReport(applied=apply)
    discovered, report.validation_failures, report.secret_redaction_findings = (
        _validated_discovery(discovered, mgr)
    )
    if report.validation_failures:
        report.applied = False
        return report
    existing = {ci.id: ci for ci in mgr.list_cis()}
    by_id: dict[str, DiscoveredCI] = {}
    migrations: list[tuple[str, str]] = []
    for item in discovered:
        ci_id = item.ci_id
        if item.ci_type in (CIType.HOST.value, CIType.DEVICE.value):
            matching_ids = _matching_identity_ids(item, existing.values())
            matching = [existing[cid] for cid in matching_ids if cid in existing]
            if matching:
                same_type = [ci for ci in matching if ci.ci_type == item.ci_type]
                target = existing.get(ci_id) or (
                    sorted(same_type, key=lambda ci: ci.id)[0] if same_type else None
                )
                # A managed host sighting promotes an alias-matched unmanaged
                # device by creating the host core and retaining the device as
                # alias history. It never rewrites a write-once CI type.
                if target is not None:
                    ci_id = target.id
                migrations.extend(
                    (duplicate.id, ci_id)
                    for duplicate in matching
                    if duplicate.id != ci_id
                )
        by_id[ci_id] = item

    for ci_id, item in by_id.items():
        current = existing.get(ci_id)
        derived_status = _observed_status(item)
        if current is None:
            # A brand-new CI has no status events yet; core.json defaults to
            # operational. Only an explicit DOWN/Failed write is needed, and only
            # for observed discoveries.
            report.created.append(ci_id)
            if apply:
                attributes = {
                    **item.attributes,
                    "canonical_name": item.canonical_name or item.name,
                    "aliases": list(item.identity_aliases),
                    "source_authority": item.authority
                    or (AUTHORITY_OBSERVED if item.observed else AUTHORITY_DECLARED),
                    "observation_schema_version": item.schema_version,
                }
                if item.lifecycle_scope:
                    attributes["lifecycle_scope"] = item.lifecycle_scope
                if item.observed_at:
                    attributes.update(
                        {
                            "observed_at": item.observed_at,
                            "scan_id": item.scan_id,
                        }
                    )
                mgr.create_ci(
                    item.name,
                    item.ci_type,
                    description=item.description,
                    node=item.node,
                    attributes=attributes,
                    tags=list(item.tags),
                    ci_id=ci_id,
                )
                if item.authority:
                    for tag in item.tags:
                        mgr.add_tag(ci_id, agent, tag, authority=item.authority)
                if derived_status and derived_status != CIStatus.OPERATIONAL.value:
                    mgr.set_status(
                        ci_id, agent, derived_status, note="from observed active_state"
                    )
                for rel_type, target in item.relationships:
                    report.relationships.append(
                        {
                            "ci_id": ci_id,
                            "action": "add",
                            "rel_type": rel_type,
                            "target": target,
                        }
                    )
                    mgr.add_relationship(
                        ci_id, agent, rel_type, target, authority=item.authority
                    )
            else:
                report.relationships.extend(
                    {
                        "ci_id": ci_id,
                        "action": "add",
                        "rel_type": rel_type,
                        "target": target,
                    }
                    for rel_type, target in item.relationships
                )
            continue

        evidence = {
            "canonical_name": item.canonical_name or item.name,
            "aliases": list(item.identity_aliases),
            "source_authority": item.authority
            or (AUTHORITY_OBSERVED if item.observed else AUTHORITY_DECLARED),
            "observation_schema_version": item.schema_version,
        }
        if item.lifecycle_scope:
            evidence["lifecycle_scope"] = item.lifecycle_scope
        if item.observed_at:
            evidence.update(
                {
                    "observed_at": item.observed_at,
                    "scan_id": item.scan_id,
                }
            )
        desired_attributes = {**item.attributes, **evidence}
        changed = [
            key
            for key, value in desired_attributes.items()
            if current.attributes.get(key) != value
        ]
        missing_rels = [
            (rel_type, target)
            for rel_type, target in item.relationships
            if not any(
                r.rel_type == rel_type
                and r.target == target
                and (not item.authority or r.authority in ("", item.authority))
                for r in current.relationships
            )
        ]
        stale_rels = [
            (rel.rel_type, rel.target)
            for rel in current.relationships
            if item.authority
            and rel.authority == item.authority
            and (rel.rel_type, rel.target) not in item.relationships
        ]
        desired_metadata = {
            "name": item.name,
            "description": item.description,
            "node": item.node,
        }
        metadata_changes = {
            key: value
            for key, value in desired_metadata.items()
            if value and getattr(current, key) != value
        }
        desired_tags = set(item.tags)
        missing_tags = sorted(desired_tags - set(current.tags))
        stale_tags = sorted(
            tag
            for tag, owner in current.tag_authorities.items()
            if owner == item.authority and tag not in desired_tags
        )
        status_changes = (
            derived_status is not None
            and current.status != CIStatus.RETIRED.value
            and current.status != derived_status
        )
        if (
            not any(
                (
                    changed,
                    missing_rels,
                    stale_rels,
                    metadata_changes,
                    missing_tags,
                    stale_tags,
                )
            )
            and not status_changes
        ):
            report.unchanged.append(ci_id)
            continue

        report_changes = sorted(changed)
        if status_changes:
            report_changes = ["status"] + report_changes
        report.updated[ci_id] = report_changes + [f"{r}->{t}" for r, t in missing_rels]
        report.updated[ci_id].extend(f"remove:{r}->{t}" for r, t in stale_rels)
        report.updated[ci_id].extend(
            f"metadata:{key}" for key in sorted(metadata_changes)
        )
        report.updated[ci_id].extend(f"tag:+{tag}" for tag in missing_tags)
        report.updated[ci_id].extend(f"tag:-{tag}" for tag in stale_tags)
        report.relationships.extend(
            {"ci_id": ci_id, "action": "add", "rel_type": rel_type, "target": target}
            for rel_type, target in missing_rels
        )
        report.relationships.extend(
            {"ci_id": ci_id, "action": "remove", "rel_type": rel_type, "target": target}
            for rel_type, target in stale_rels
        )
        if apply:
            for key in changed:
                mgr.set_attribute(ci_id, agent, key, desired_attributes[key])
            if status_changes:
                mgr.set_status(
                    ci_id, agent, derived_status, note="from observed active_state"
                )
            for rel_type, target in missing_rels:
                mgr.add_relationship(
                    ci_id, agent, rel_type, target, authority=item.authority
                )
            for rel_type, target in stale_rels:
                mgr.remove_relationship(ci_id, agent, rel_type, target)
            for key, value in metadata_changes.items():
                mgr.set_metadata(ci_id, agent, key, value)
            for tag in missing_tags:
                mgr.add_tag(ci_id, agent, tag, authority=item.authority)
            for tag in stale_tags:
                mgr.remove_tag(ci_id, agent, tag, authority=item.authority)

    if not scan_complete:
        return report

    for ci_id, ci in existing.items():
        if ci_id in by_id:
            continue
        if any(duplicate_id == ci_id for duplicate_id, _ in migrations):
            continue
        if DISCOVERED_TAG in (ci.tags or []) and ci.status != CIStatus.RETIRED.value:
            report.orphans.append(ci_id)

    for duplicate_id, canonical_id in sorted(set(migrations)):
        duplicate = existing[duplicate_id]
        relation = f"alias_of->{canonical_id}"
        if any(
            rel.rel_type == "alias_of" and rel.target == canonical_id
            for rel in duplicate.relationships
        ):
            continue
        report.updated.setdefault(duplicate_id, []).append(relation)
        report.relationships.append(
            {
                "ci_id": duplicate_id,
                "action": "add",
                "rel_type": "alias_of",
                "target": canonical_id,
            }
        )
        if apply:
            mgr.add_relationship(duplicate_id, agent, "alias_of", canonical_id)

    return report
