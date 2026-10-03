"""Explicit, audit-gated enrollment of legacy CIs into discovery ownership."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .atomic_io import atomic_write_text
from .cmdb import CIStatus, CMDBManager
from .discovery import DISCOVERED_TAG

_RUN_SCHEMA = "skcoord.cmdb.reconcile-run/v1"
_BACKFILL_SCHEMA = "skcoord.cmdb.ownership-backfill/v1"


@dataclass(frozen=True)
class OwnershipEntry:
    """One operator-reviewed legacy CI enrollment."""

    ci_id: str
    authority: str
    scope_fingerprint: str


@dataclass(frozen=True)
class ShadowGate:
    eligible: bool
    scope_fingerprint: str
    scan_ids: tuple[str, ...]
    errors: tuple[str, ...]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _read_verified_artifact(path: Path) -> tuple[dict, str | None]:
    path = Path(path)
    try:
        payload = path.read_bytes()
        artifact = json.loads(payload)
    except (OSError, ValueError) as exc:
        return {}, f"{path}: unreadable artifact: {exc}"
    sidecar = path.with_suffix(".sha256")
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        return {}, f"{path}: missing or invalid checksum sidecar: {exc}"
    actual = hashlib.sha256(payload).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or expected != actual:
        return {}, f"{path}: checksum mismatch"
    return artifact, None


def evaluate_shadow_gate(paths: Sequence[Path], minimum_runs: int = 3) -> ShadowGate:
    """Require distinct complete dry runs over one exact target/collector scope."""
    errors: list[str] = []
    scans: list[str] = []
    scopes: list[str] = []
    if len(paths) < minimum_runs:
        errors.append(f"need at least {minimum_runs} shadow artifacts, got {len(paths)}")
    for path in paths:
        artifact, error = _read_verified_artifact(Path(path))
        if error:
            errors.append(error)
            continue
        scan_id = str(artifact.get("scan_id", ""))
        scope = str(artifact.get("scope_fingerprint", ""))
        if artifact.get("schema") != _RUN_SCHEMA:
            errors.append(f"{path}: unsupported schema")
        if artifact.get("applied") is not False:
            errors.append(f"{path}: shadow artifact must have applied=false")
        if artifact.get("completeness", {}).get("complete") is not True:
            errors.append(f"{path}: scan is incomplete")
        if not scan_id:
            errors.append(f"{path}: missing scan_id")
        elif scan_id in scans:
            errors.append(f"{path}: duplicate scan_id {scan_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", scope):
            errors.append(f"{path}: invalid scope_fingerprint")
        scans.append(scan_id)
        scopes.append(scope)
    distinct_scopes = {scope for scope in scopes if scope}
    if len(distinct_scopes) > 1:
        errors.append("shadow artifacts cover different target/collector scopes")
    scope = next(iter(distinct_scopes), "")
    return ShadowGate(not errors, scope, tuple(scans), tuple(errors))


def load_manifest(path: Path) -> list[OwnershipEntry]:
    """Load the deliberately small, human-reviewable ownership manifest."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != _BACKFILL_SCHEMA or not isinstance(data.get("entries"), list):
        raise ValueError(f"manifest schema must be {_BACKFILL_SCHEMA}")
    entries = [OwnershipEntry(**item) for item in data["entries"]]
    if len({entry.ci_id for entry in entries}) != len(entries):
        raise ValueError("manifest contains duplicate ci_id entries")
    return entries


def _ci_state_digest(ci: object) -> str:
    """Bind approval to the complete folded state visible during preview."""
    state = {
        "id": getattr(ci, "id", ""),
        "ci_type": getattr(ci, "ci_type", ""),
        "name": getattr(ci, "name", ""),
        "status": getattr(ci, "status", ""),
        "attributes": getattr(ci, "attributes", {}),
        "tags": getattr(ci, "tags", []),
        "tag_authorities": getattr(ci, "tag_authorities", {}),
    }
    return hashlib.sha256(_canonical(state).encode()).hexdigest()


def plan_ownership_backfill(
    mgr: CMDBManager, entries: Sequence[OwnershipEntry], gate: ShadowGate
) -> dict:
    """Build a deterministic plan; reject implicit, stale, or unsafe enrollment."""
    errors: list[str] = list(gate.errors)
    actions: list[dict] = []
    for entry in sorted(entries, key=lambda item: item.ci_id):
        ci = mgr.get_ci(entry.ci_id)
        if ci is None:
            errors.append(f"{entry.ci_id}: CI does not exist")
            continue
        if not entry.authority.strip():
            errors.append(f"{entry.ci_id}: authority is required")
        if entry.scope_fingerprint != gate.scope_fingerprint:
            errors.append(f"{entry.ci_id}: scope does not match shadow gate")
        if ci.status == CIStatus.RETIRED.value:
            errors.append(f"{entry.ci_id}: retired CI cannot be enrolled")
        if DISCOVERED_TAG in ci.tags:
            errors.append(f"{entry.ci_id}: already discovery-owned")
        actions.append({**asdict(entry), "ci_state_digest": _ci_state_digest(ci)})
    body = {
        "schema": _BACKFILL_SCHEMA,
        "shadow_scan_ids": list(gate.scan_ids),
        "scope_fingerprint": gate.scope_fingerprint,
        "actions": actions,
    }
    return {
        **body,
        "eligible": gate.eligible and not errors and bool(actions),
        "errors": errors,
        "plan_digest": hashlib.sha256(_canonical(body).encode()).hexdigest(),
    }


def apply_ownership_backfill(
    mgr: CMDBManager,
    plan: dict,
    approval_digest: str,
    *,
    agent: str = "cmdb-ownership-backfill",
) -> Path:
    """Apply only the exact reviewed plan and persist a checksummed audit record."""
    if not plan.get("eligible"):
        raise ValueError("ownership plan did not pass its audit gates")
    if not approval_digest or approval_digest != plan.get("plan_digest"):
        raise ValueError("approval digest does not match the reviewed plan")
    checked = []
    for action in plan["actions"]:
        ci_id, authority = action["ci_id"], action["authority"]
        current = mgr.get_ci(ci_id)
        if (
            current is None
            or DISCOVERED_TAG in current.tags
            or _ci_state_digest(current) != action.get("ci_state_digest")
        ):
            raise RuntimeError(f"{ci_id}: CMDB changed after plan review; aborting")
        checked.append((ci_id, authority, action["scope_fingerprint"]))
    # Validate the complete batch before its first append. CMDB event files do
    # not provide a cross-CI transaction, so this prevents known partial plans.
    for ci_id, authority, scope_fingerprint in checked:
        mgr.add_tag(ci_id, agent, DISCOVERED_TAG, authority=authority)
        mgr.set_attribute(ci_id, agent, "source_authority", authority)
        mgr.set_attribute(ci_id, agent, "lifecycle_scope", scope_fingerprint)
    audit = {**plan, "applied": True, "approval_digest": approval_digest}
    directory = mgr.cmdb_dir / "ownership-backfills"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{approval_digest}.json"
    payload = _canonical(audit) + "\n"
    atomic_write_text(path, payload)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    atomic_write_text(path.with_suffix(".sha256"), f"{checksum}  {path.name}\n")
    return path
