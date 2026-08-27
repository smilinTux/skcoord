"""CMDB / asset management: event-sourced Configuration Items + relationships.

Phase 6 of the SKDashboard, and the missing ITIL leg. Configuration Items (CIs)
are services, hosts, agents, credentials, ports, datastores. Each is stored the
same conflict-free way as ITIL: an immutable ``core.json`` (write-once) plus
append-only per-writer ``events/<agent>@<host>.jsonl``, folded on read. CIs carry
relationships (depends_on / runs_on / hosts / connects_to) so we get a dependency
graph and impact analysis: which CIs a failure cascades to, and which open
incidents affect a CI (via the incident's affected_services).

    ~/.skcapstone/cmdb/<ci_id>/{core.json, events/<agent>@<host>.jsonl}
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import socket
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.cmdb")

_HOST = socket.gethostname()
CMDB_CORE_SCHEMA_VERSION = 2
CMDB_EVENT_SCHEMA_VERSION = 2
SECRET_ATTRIBUTE_KEY_FRAGMENTS = (
    "secret",
    "password",
    "passphrase",
    "token",
    "credential",
    "private_key",
    "api_key",
    "authorization",
)


class CIType(str, Enum):
    SERVICE = "service"
    HOST = "host"
    AGENT = "agent"
    CREDENTIAL = "credential"
    PORT = "port"
    DATASTORE = "datastore"
    NETWORK = "network"
    DEVICE = "device"


class CIStatus(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    RETIRED = "retired"


class Relationship(BaseModel):
    rel_type: str = "depends_on"  # depends_on | runs_on | hosts | connects_to
    target: str  # target CI id
    authority: str = ""


ALLOWED_RELATIONSHIPS: dict[str, set[str]] = {
    "depends_on": {item.value for item in CIType},
    "runs_on": {CIType.HOST.value},
    "hosts": {item.value for item in CIType},
    "connects_to": {item.value for item in CIType},
    # Identity reconciliation preserves an old record as an alias instead of
    # rewriting its write-once core.
    "alias_of": {CIType.HOST.value, CIType.DEVICE.value},
}


class FutureSchemaVersionError(ValueError):
    """The store contains data newer than this reader understands."""


class SecretAttributeKeyError(ValueError):
    """A CMDB write attempted to persist a secret-looking attribute key."""


def is_secret_attribute_key(key: object) -> bool:
    """Return whether *key* contains a denied secret-key fragment.

    Matching is deliberately case-insensitive and substring-based. This errs on
    the safe side for the append-only CMDB: names such as ``csrf_token_name``
    are rejected along with ``API_TOKEN`` rather than relying on callers to
    classify whether the associated value is itself sensitive.
    """
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SECRET_ATTRIBUTE_KEY_FRAGMENTS)


def _secret_attribute_paths(value: object, path: str = "attributes") -> list[str]:
    """Return secret-looking mapping-key paths without inspecting string values."""
    findings: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if is_secret_attribute_key(key):
                findings.append(child_path)
            findings.extend(_secret_attribute_paths(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            findings.extend(_secret_attribute_paths(child, f"{path}[{index}]"))
    return findings


def _validate_attribute_write(value: object, *, ci_id: str, agent: str) -> None:
    """Reject secret-looking attribute keys before an immutable write occurs."""
    findings = _secret_attribute_paths(value)
    if not findings:
        return
    logger.warning(
        "CMDB secret-looking attribute key rejected (ci_id=%s agent=%s paths=%s)",
        ci_id,
        agent,
        ",".join(findings),
    )
    raise SecretAttributeKeyError(
        "CMDB attributes contain forbidden secret-looking key(s): "
        + ", ".join(findings)
    )


def _schema_version(value: Any, *, supported: int, record: str) -> int:
    """Parse a schema version and reject future data instead of mis-folding it."""
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid schema_version for {record}: {value!r}") from exc
    if version < 1:
        raise ValueError(f"invalid schema_version for {record}: {version}")
    if version > supported:
        raise FutureSchemaVersionError(
            f"{record} schema v{version} is newer than supported v{supported}"
        )
    return version


class ConfigItem(BaseModel):
    id: str
    ci_type: str = CIType.SERVICE.value
    name: str
    status: str = CIStatus.OPERATIONAL.value
    description: str = ""
    owner: str = ""
    node: str = ""  # host it runs on (for services)
    attributes: dict = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 1
    tag_authorities: dict[str, str] = Field(default_factory=dict, exclude=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "ci"


def make_ci_id(ci_type: str, name: str) -> str:
    """Deterministic id so re-seeding the same CI is idempotent."""
    return f"ci-{ci_type}-{_slug(name)}"


class CMDBManager:
    """Event-sourced Configuration Management Database."""

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.cmdb_dir = self.home / "cmdb"

    def ensure_dirs(self) -> None:
        self.cmdb_dir.mkdir(parents=True, exist_ok=True)

    def _writer_id(self, agent: str) -> str:
        safe = (agent or "unknown").replace("/", "-").replace("@", "-")
        return f"{safe}@{_HOST}"

    # ── writes ────────────────────────────────────────────────────────────

    def create_ci(
        self,
        name: str,
        ci_type: str = "service",
        description: str = "",
        owner: str = "",
        node: str = "",
        attributes: Optional[dict] = None,
        tags: Optional[list] = None,
        ci_id: Optional[str] = None,
    ) -> ConfigItem:
        """Create (or return existing) a CI. Write-once core, idempotent by id."""
        cid = ci_id or make_ci_id(ci_type, name)
        clean_attributes = attributes or {}
        _validate_attribute_write(clean_attributes, ci_id=cid, agent="create")
        self.ensure_dirs()
        core = {
            "schema_version": CMDB_CORE_SCHEMA_VERSION,
            "id": cid,
            "ci_type": ci_type,
            "name": name,
            "description": description,
            "owner": owner,
            "node": node,
            "attributes": clean_attributes,
            "tags": tags or [],
            "created_at": _now_iso(),
        }
        rec_dir = self.cmdb_dir / cid
        rec_dir.mkdir(parents=True, exist_ok=True)
        core_path = rec_dir / "core.json"
        payload = (json.dumps(core, indent=2, default=str) + "\n").encode("utf-8")
        try:
            fd = os.open(str(core_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self.get_ci(cid)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return self.get_ci(cid)

    def _append(self, ci_id: str, agent: str, action: str, **payload: Any) -> None:
        rec_dir = self.cmdb_dir / ci_id
        events_dir = rec_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{self._writer_id(agent)}.jsonl"
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seq = sum(1 for _ in fh)
                event = {
                    "schema_version": CMDB_EVENT_SCHEMA_VERSION,
                    "ts": _now_iso(),
                    "writer": agent,
                    "node": _HOST,
                    "seq": seq,
                    "action": action,
                }
                event.update(payload)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(event, default=str) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def set_status(self, ci_id: str, agent: str, status: str, note: str = "") -> None:
        self._append(ci_id, agent, "status", status=status, note=note)

    def set_attribute(self, ci_id: str, agent: str, key: str, value: Any) -> None:
        _validate_attribute_write({key: value}, ci_id=ci_id, agent=agent)
        self._append(ci_id, agent, "attribute", key=key, value=value)

    def add_relationship(
        self, ci_id: str, agent: str, rel_type: str, target: str, authority: str = ""
    ) -> None:
        self._append(
            ci_id,
            agent,
            "relate",
            rel_type=rel_type,
            target=target,
            authority=authority,
        )

    def remove_relationship(
        self, ci_id: str, agent: str, rel_type: str, target: str
    ) -> None:
        self._append(ci_id, agent, "unrelate", rel_type=rel_type, target=target)

    def set_metadata(self, ci_id: str, agent: str, key: str, value: str) -> None:
        """Update a mutable display/placement field without rewriting core.json."""
        if key not in {"name", "description", "owner", "node"}:
            raise ValueError(f"unsupported mutable metadata field: {key}")
        self._append(ci_id, agent, "metadata", key=key, value=value)

    def add_tag(self, ci_id: str, agent: str, tag: str, authority: str = "") -> None:
        self._append(ci_id, agent, "tag", tag=tag, authority=authority)

    def remove_tag(self, ci_id: str, agent: str, tag: str, authority: str = "") -> None:
        self._append(ci_id, agent, "untag", tag=tag, authority=authority)

    # ── reads ─────────────────────────────────────────────────────────────

    def _read_events(self, ci_id: str) -> list[dict]:
        events_dir = self.cmdb_dir / ci_id / "events"
        out: list[dict] = []
        if not events_dir.exists():
            return out
        for f in sorted(events_dir.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    _schema_version(
                        event.get("schema_version", 1),
                        supported=CMDB_EVENT_SCHEMA_VERSION,
                        record=f"event {f.name}",
                    )
                    out.append(event)
        out.sort(key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
        return out

    def get_ci(self, ci_id: str) -> Optional[ConfigItem]:
        core_path = self.cmdb_dir / ci_id / "core.json"
        if not core_path.exists():
            return None
        try:
            core = json.loads(core_path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        version = _schema_version(
            core.get("schema_version", 1),
            supported=CMDB_CORE_SCHEMA_VERSION,
            record=f"CI {ci_id}",
        )
        ci = ConfigItem(
            id=core["id"],
            ci_type=core.get("ci_type", "service"),
            name=core.get("name", ""),
            description=core.get("description", ""),
            owner=core.get("owner", ""),
            node=core.get("node", ""),
            attributes=dict(core.get("attributes", {})),
            tags=list(core.get("tags", [])),
            created_at=core.get("created_at", ""),
            # Version 1 is the deployed, unversioned format.  Missing is not
            # corruption: it is the explicit backwards-compatible migration.
            schema_version=version,
        )
        for e in self._read_events(ci_id):
            act = e.get("action")
            if act == "status":
                ci.status = e.get("status", ci.status)
            elif act == "attribute" and e.get("key"):
                ci.attributes[e["key"]] = e.get("value")
            elif act == "relate":
                rel = Relationship(
                    rel_type=e.get("rel_type", "depends_on"),
                    target=e.get("target", ""),
                    authority=e.get("authority", ""),
                )
                if rel.target:
                    ci.relationships = [
                        current
                        for current in ci.relationships
                        if not (
                            current.rel_type == rel.rel_type
                            and current.target == rel.target
                        )
                    ]
                    ci.relationships.append(rel)
            elif act == "unrelate":
                ci.relationships = [
                    r
                    for r in ci.relationships
                    if not (
                        r.rel_type == e.get("rel_type") and r.target == e.get("target")
                    )
                ]
            elif act == "metadata" and e.get("key") in {
                "name",
                "description",
                "owner",
                "node",
            }:
                setattr(ci, e["key"], str(e.get("value", "")))
            elif act == "tag" and e.get("tag"):
                tag = str(e["tag"])
                if tag not in ci.tags:
                    ci.tags.append(tag)
                if e.get("authority"):
                    ci.tag_authorities[tag] = str(e["authority"])
            elif act == "untag" and e.get("tag"):
                tag = str(e["tag"])
                if not e.get("authority") or ci.tag_authorities.get(tag) == e.get(
                    "authority"
                ):
                    ci.tags = [item for item in ci.tags if item != tag]
                    ci.tag_authorities.pop(tag, None)
            ci.updated_at = e.get("ts", ci.updated_at)
        return ci

    def migration_preview(self, ci_id: str) -> dict[str, Any]:
        """Return a detached v2 representation without modifying the store.

        This is the safe primitive for a v1-to-v2 migration command: callers
        can validate and write the returned copy to a separate destination
        before any cutover. Future-version records fail closed.
        """
        core_path = self.cmdb_dir / ci_id / "core.json"
        if not core_path.exists():
            raise KeyError(ci_id)
        core = json.loads(core_path.read_text(encoding="utf-8"))
        if not isinstance(core, dict):
            raise ValueError(f"invalid core for CI {ci_id}")
        source_version = _schema_version(
            core.get("schema_version", 1),
            supported=CMDB_CORE_SCHEMA_VERSION,
            record=f"CI {ci_id}",
        )
        migrated_core = dict(core)
        migrated_core["schema_version"] = CMDB_CORE_SCHEMA_VERSION
        events = []
        for event in self._read_events(ci_id):
            migrated = dict(event)
            migrated["schema_version"] = CMDB_EVENT_SCHEMA_VERSION
            events.append(migrated)
        return {
            "ci_id": ci_id,
            "source_schema_version": source_version,
            "target_schema_version": CMDB_CORE_SCHEMA_VERSION,
            "core": migrated_core,
            "events": events,
        }

    def migrate_schema(
        self,
        *,
        apply: bool = False,
        backup_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Safely migrate the physical CMDB store from schema v1 to v2.

        Dry-run is the default and performs no filesystem writes.  Applying a
        migration copies the complete store to a same-filesystem staging
        directory, rewrites and validates the staged copy, then atomically
        renames the original to a retained backup and the staging directory
        into place.  A failure during cutover restores the original path.

        Records already at v2 are left byte-for-byte alone, making repeated
        calls idempotent.  Unknown future versions and malformed records abort
        before the live store is renamed.
        """
        plan = self._schema_migration_plan()
        result: dict[str, Any] = {
            "applied": False,
            "source": str(self.cmdb_dir),
            "target_schema_version": CMDB_CORE_SCHEMA_VERSION,
            **plan,
        }
        if not apply or plan["records_to_migrate"] == 0:
            return result

        parent = self.cmdb_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        backup = (
            Path(backup_path).expanduser()
            if backup_path
            else parent
            / (
                f"cmdb.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
            )
        )
        if backup.parent != parent:
            raise ValueError("backup_path must share the CMDB parent filesystem")
        if backup.exists():
            raise FileExistsError(f"backup path already exists: {backup}")

        lock_path = parent / ".cmdb-schema-migration.lock"
        with open(lock_path, "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # Re-plan under the lock so a concurrent writer cannot invalidate
            # the dry-run result used to decide whether migration is needed.
            plan = self._schema_migration_plan()
            result.update(plan)
            if plan["records_to_migrate"] == 0:
                return result
            staging = Path(tempfile.mkdtemp(prefix=".cmdb-migrate-", dir=parent))
            shutil.rmtree(staging)
            moved_original = False
            try:
                shutil.copytree(self.cmdb_dir, staging, symlinks=True)
                staged = CMDBManager(parent)
                staged.cmdb_dir = staging
                staged._rewrite_schema_v2()
                staged_plan = staged._schema_migration_plan()
                if staged_plan["records_to_migrate"]:
                    raise RuntimeError("staged CMDB did not converge to schema v2")
                # Folding every CI validates both core and event records.
                staged.list_cis()
                os.rename(self.cmdb_dir, backup)
                moved_original = True
                try:
                    os.rename(staging, self.cmdb_dir)
                except BaseException:
                    os.rename(backup, self.cmdb_dir)
                    moved_original = False
                    raise
                result.update(applied=True, backup=str(backup))
                return result
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
                if moved_original and not self.cmdb_dir.exists() and backup.exists():
                    os.rename(backup, self.cmdb_dir)

    def _schema_migration_plan(self) -> dict[str, Any]:
        """Validate all physical records and report the v1 work required."""
        records = cores = events = 0
        if not self.cmdb_dir.exists():
            return {"records": 0, "records_to_migrate": 0, "cores": 0, "events": 0}
        for record in sorted(self.cmdb_dir.iterdir()):
            core_path = record / "core.json"
            if not record.is_dir() or not core_path.exists():
                continue
            if record.is_symlink() or core_path.is_symlink():
                raise ValueError(f"refusing symlinked CMDB record: {record}")
            core = json.loads(core_path.read_text(encoding="utf-8"))
            if not isinstance(core, dict):
                raise ValueError(f"invalid core for CI {record.name}")
            version = _schema_version(
                core.get("schema_version", 1),
                supported=CMDB_CORE_SCHEMA_VERSION,
                record=f"CI {record.name}",
            )
            records += 1
            if version < CMDB_CORE_SCHEMA_VERSION:
                cores += 1
            event_dir = record / "events"
            if event_dir.exists():
                for path in sorted(event_dir.glob("*.jsonl")):
                    if path.is_symlink():
                        raise ValueError(f"refusing symlinked CMDB event: {path}")
                    for number, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(), 1
                    ):
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError(f"invalid event {path}:{number}")
                        version = _schema_version(
                            event.get("schema_version", 1),
                            supported=CMDB_EVENT_SCHEMA_VERSION,
                            record=f"event {path.name}:{number}",
                        )
                        if version < CMDB_EVENT_SCHEMA_VERSION:
                            events += 1
        return {
            "records": records,
            "records_to_migrate": cores + events,
            "cores": cores,
            "events": events,
        }

    def _rewrite_schema_v2(self) -> None:
        """Rewrite v1 records in a staging tree; never call on the live tree."""
        for record in sorted(self.cmdb_dir.iterdir()):
            core_path = record / "core.json"
            if not record.is_dir() or not core_path.exists():
                continue
            core = json.loads(core_path.read_text(encoding="utf-8"))
            if int(core.get("schema_version", 1)) < CMDB_CORE_SCHEMA_VERSION:
                core["schema_version"] = CMDB_CORE_SCHEMA_VERSION
                core_path.write_text(
                    json.dumps(core, indent=2, default=str) + "\n", encoding="utf-8"
                )
            event_dir = record / "events"
            if not event_dir.exists():
                continue
            for path in sorted(event_dir.glob("*.jsonl")):
                output = []
                changed = False
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        output.append(line)
                        continue
                    event = json.loads(line)
                    if int(event.get("schema_version", 1)) < CMDB_EVENT_SCHEMA_VERSION:
                        event["schema_version"] = CMDB_EVENT_SCHEMA_VERSION
                        line = json.dumps(event, default=str)
                        changed = True
                    output.append(line)
                if changed:
                    path.write_text("\n".join(output) + "\n", encoding="utf-8")

    def list_cis(self, ci_type: Optional[str] = None) -> list[ConfigItem]:
        if not self.cmdb_dir.exists():
            return []
        out = []
        for p in sorted(self.cmdb_dir.iterdir()):
            if not (p.is_dir() and (p / "core.json").exists()):
                continue
            ci = self.get_ci(p.name)
            if ci and (ci_type is None or ci.ci_type == ci_type):
                out.append(ci)
        return out

    def find_for_service(self, service_name: str) -> Optional[ConfigItem]:
        cid = make_ci_id(CIType.SERVICE.value, service_name)
        return self.get_ci(cid)

    def seed_from_inventory(self, agent: str = "cmdb-seed") -> dict:
        """Compatibility bridge to the versioned discovery reconciler.

        This method remains only for older dashboard clients that still call
        ``POST /api/cmdb/seed``. It imports declared fleet, registry, and agent
        sources through the same schema-driven reconciler as the supported CLI;
        it never invents hosts or derives asset health from ITIL incidents.
        """
        from .discovery import reconcile, scan

        discovered = scan(self.home, runners=(), include_declared=True)
        report = reconcile(
            self, discovered, agent=agent, apply=True, scan_complete=False
        )
        result = report.as_dict()
        result.update(
            {
                "schema": "skcoord.cmdb.compat-seed/v1",
                "deprecated": True,
                "cis": len(self.list_cis()),
                "touched": len(report.created) + len(report.updated),
            }
        )
        return result

    def impact_analysis(self, ci_id: str) -> dict:
        """What depends on this CI (cascade) + which open incidents affect it."""
        ci = self.get_ci(ci_id)
        if ci is None:
            return {"error": "CI not found", "id": ci_id}
        dependents = []
        for other in self.list_cis():
            for rel in other.relationships:
                if rel.target == ci_id and rel.rel_type in ("depends_on", "runs_on"):
                    dependents.append(
                        {
                            "id": other.id,
                            "name": other.name,
                            "ci_type": other.ci_type,
                            "rel": rel.rel_type,
                        }
                    )
        incidents = []
        try:
            from .itil import ITILManager

            mgr = ITILManager(self.home)
            for inc in mgr.list_incidents():
                if inc.status.value in ("resolved", "closed"):
                    continue
                if ci.name in (inc.affected_services or []) or ci_id in (
                    inc.affected_services or []
                ):
                    incidents.append(
                        {
                            "id": inc.id,
                            "severity": inc.severity.value,
                            "status": inc.status.value,
                            "title": inc.title,
                        }
                    )
        except Exception:  # noqa: BLE001
            pass
        return {
            "ci": ci.model_dump(),
            "dependents": dependents,
            "open_incidents": incidents,
        }

    def audit_relationships(self) -> list[dict[str, Any]]:
        """Return deterministic relationship-integrity findings without writing."""
        cis = {ci.id: ci for ci in self.list_cis()}
        findings: list[dict[str, Any]] = []
        for source_id in sorted(cis):
            source = cis[source_id]
            for rel in sorted(
                source.relationships, key=lambda r: (r.rel_type, r.target)
            ):
                target = cis.get(rel.target)
                base = {
                    "source": source_id,
                    "relationship": rel.rel_type,
                    "target": rel.target,
                }
                if target is None:
                    findings.append({"kind": "dangling_target", **base})
                    continue
                if source_id == rel.target:
                    findings.append({"kind": "self_edge", **base})
                allowed = ALLOWED_RELATIONSHIPS.get(rel.rel_type)
                if allowed is None:
                    findings.append({"kind": "unknown_relationship", **base})
                elif target.ci_type not in allowed:
                    findings.append(
                        {
                            "kind": "invalid_target_type",
                            **base,
                            "target_type": target.ci_type,
                        }
                    )
        return findings

    def impact_graph(
        self, ci_id: str, max_depth: int = 8, max_nodes: int = 1000
    ) -> dict:
        """Return transitive dependents with cycle and fan-out protection."""
        if max_depth < 0 or max_nodes < 1:
            raise ValueError("max_depth must be >= 0 and max_nodes must be >= 1")
        cis = {ci.id: ci for ci in self.list_cis()}
        if ci_id not in cis:
            return {"error": "CI not found", "id": ci_id}
        reverse: dict[str, list[tuple[str, str]]] = {}
        for source in cis.values():
            for rel in source.relationships:
                if rel.rel_type in ("depends_on", "runs_on"):
                    reverse.setdefault(rel.target, []).append((source.id, rel.rel_type))
        queue: list[tuple[str, int, tuple[str, ...]]] = [(ci_id, 0, (ci_id,))]
        seen = {ci_id}
        nodes: list[dict[str, Any]] = []
        cycles: list[list[str]] = []
        truncated = False
        while queue:
            target, depth, path = queue.pop(0)
            if depth >= max_depth:
                truncated = truncated or bool(reverse.get(target))
                continue
            for source_id, rel_type in sorted(reverse.get(target, [])):
                if source_id in path:
                    cycles.append([*path, source_id])
                    continue
                if source_id in seen:
                    continue
                if len(nodes) >= max_nodes:
                    truncated = True
                    queue.clear()
                    break
                seen.add(source_id)
                source = cis[source_id]
                nodes.append(
                    {
                        "id": source_id,
                        "name": source.name,
                        "ci_type": source.ci_type,
                        "rel": rel_type,
                        "depth": depth + 1,
                    }
                )
                queue.append((source_id, depth + 1, (*path, source_id)))
        return {
            "id": ci_id,
            "dependents": nodes,
            "cycles": cycles,
            "truncated": truncated,
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }
