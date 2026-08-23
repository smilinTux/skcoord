"""Policy and evidence helpers for scheduled CMDB reconciliation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .atomic_io import atomic_write_text
from .cmdb import CIType, make_ci_id
from .itil import ITILManager

_SCHEMA = "skcoord.cmdb.reconcile-job-config/v1"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ScheduledReconcileConfig:
    """Validated configuration for one fleet reconciliation authority."""

    enabled: bool = False
    owner_node: str = "chiap04"
    agent: str = "jarvis"
    cadence_seconds: int = 900
    targets: tuple[str, ...] = ()
    credential_refs: Mapping[str, str] = field(default_factory=dict)
    global_concurrency: int = 4
    per_host_concurrency: int = 1
    timeout_seconds: float = 180.0
    failure_budget: int = 0
    retry_count: int = 2
    retry_backoff_seconds: float = 30.0
    retention_runs: int = 96
    stale_grace_runs: int = 3
    drift_alert_runs: int = 2
    failure_alert_runs: int = 3
    apply_safe_observations: bool = True

    def __post_init__(self) -> None:
        if not self.owner_node.strip() or not self.agent.strip():
            raise ValueError("owner_node and agent are required")
        if not _NAME_RE.fullmatch(self.owner_node) or not _NAME_RE.fullmatch(self.agent):
            raise ValueError("owner_node and agent must be safe names")
        positive = (
            self.cadence_seconds,
            self.global_concurrency,
            self.per_host_concurrency,
            self.retention_runs,
            self.stale_grace_runs,
            self.drift_alert_runs,
            self.failure_alert_runs,
        )
        if min(positive) < 1 or self.timeout_seconds <= 0:
            raise ValueError("cadence, limits, retention, grace, and thresholds must be positive")
        if self.failure_budget < 0 or self.retry_count < 0 or self.retry_backoff_seconds < 0:
            raise ValueError("failure budget, retries, and backoff must be non-negative")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be unique")
        if self.enabled and (not self.targets or set(self.credential_refs) != set(self.targets)):
            raise ValueError("enabled jobs require one credential reference per target")
        for host, reference in self.credential_refs.items():
            if not host.strip() or not str(reference).startswith("skvault://"):
                raise ValueError("credential references must use skvault://")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ScheduledReconcileConfig:
        """Parse a mapping while rejecting unknown or structurally unsafe values."""
        schema = value.get("schema", _SCHEMA)
        if schema != _SCHEMA:
            raise ValueError(f"unsupported reconcile job config schema: {schema}")
        known = set(cls.__dataclass_fields__)
        unknown = set(value) - known - {"schema"}
        if unknown:
            raise ValueError(f"unknown reconcile job config keys: {sorted(unknown)}")
        data = {key: item for key, item in value.items() if key != "schema"}
        for key in ("enabled", "apply_safe_observations"):
            if key in data and not isinstance(data[key], bool):
                raise ValueError(f"{key} must be a boolean")
        for key in (
            "cadence_seconds",
            "global_concurrency",
            "per_host_concurrency",
            "failure_budget",
            "retry_count",
            "retention_runs",
            "stale_grace_runs",
            "drift_alert_runs",
            "failure_alert_runs",
        ):
            if key in data and (not isinstance(data[key], int) or isinstance(data[key], bool)):
                raise ValueError(f"{key} must be an integer")
        for key in ("timeout_seconds", "retry_backoff_seconds"):
            if key in data and (
                not isinstance(data[key], (int, float)) or isinstance(data[key], bool)
            ):
                raise ValueError(f"{key} must be numeric")
        for key in ("owner_node", "agent"):
            if key in data and not isinstance(data[key], str):
                raise ValueError(f"{key} must be a string")
        targets = data.get("targets", ())
        if not isinstance(targets, (list, tuple)):
            raise ValueError("targets must be an array")
        data["targets"] = tuple(str(item) for item in targets)
        refs = data.get("credential_refs", {})
        if not isinstance(refs, Mapping):
            raise ValueError("credential_refs must be an object")
        data["credential_refs"] = {str(key): str(item) for key, item in refs.items()}
        return cls(**data)

    def as_dict(self) -> dict:
        """Return the secret-free, versioned configuration representation."""
        return {
            "schema": _SCHEMA,
            **{
                key: list(value) if key == "targets" else dict(value)
                if key == "credential_refs"
                else value
                for key, value in self.__dict__.items()
            },
        }

    def fingerprint(self) -> str:
        """Return a stable config identifier without resolving credentials."""
        import hashlib

        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_reconcile_job_config(path: Path) -> ScheduledReconcileConfig:
    """Load JSON config, defaulting to a disabled job when the file is absent."""
    path = Path(path).expanduser()
    if not path.exists():
        return ScheduledReconcileConfig()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("reconcile job config must be a JSON object")
    return ScheduledReconcileConfig.from_mapping(value)


class ReconcileLease(AbstractContextManager["ReconcileLease"]):
    """Nonblocking node-local lease that prevents overlapping application."""

    def __init__(self, root: Path, owner_node: str, agent: str) -> None:
        self.path = Path(root) / "scheduler" / owner_node / "locks" / "cmdb-reconcile.lock"
        self.agent = agent
        self.acquired = False
        self._file = None

    def __enter__(self) -> ReconcileLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return self
        self.acquired = True
        self._file.seek(0)
        self._file.truncate()
        self._file.write(
            json.dumps(
                {"agent": self.agent, "host": socket.gethostname(), "pid": os.getpid()},
                sort_keys=True,
            )
            + "\n"
        )
        self._file.flush()
        os.fsync(self._file.fileno())
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._file is not None:
            if self.acquired:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
        self.acquired = False


def _event_keys(artifact: Mapping[str, object], event_group: str) -> set[str]:
    events = artifact.get("events", {})
    if not isinstance(events, Mapping):
        return set()
    records = events.get(event_group, [])
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return set()
    return {
        str(record.get("dedup_key"))
        for record in records
        if isinstance(record, Mapping) and record.get("dedup_key")
    }


def consecutive_event_count(
    artifacts: Sequence[Mapping[str, object]], event_group: str, dedup_key: str
) -> int:
    """Count newest consecutive artifacts containing one normalized event."""
    ordered = sorted(artifacts, key=lambda item: str(item.get("ended_at", "")), reverse=True)
    count = 0
    for artifact in ordered:
        if dedup_key not in _event_keys(artifact, event_group):
            break
        count += 1
    return count


def route_reconcile_incidents(
    home: Path,
    artifacts: Sequence[Mapping[str, object]],
    config: ScheduledReconcileConfig,
) -> list[str]:
    """Create deduplicated ITIL incidents for material or repeated signals."""
    if not artifacts:
        return []
    latest = max(artifacts, key=lambda item: str(item.get("ended_at", "")))
    events = latest.get("events", {})
    if not isinstance(events, Mapping):
        return []
    mgr = ITILManager(Path(home))
    incident_ids: list[str] = []
    groups = (
        ("drift", config.drift_alert_runs),
        ("scan_health", config.failure_alert_runs),
    )
    for group, default_threshold in groups:
        records = events.get(group, [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping) or not record.get("dedup_key"):
                continue
            material = group == "drift" and record.get("severity") == "high"
            threshold = 1 if material else default_threshold
            if consecutive_event_count(artifacts, group, str(record["dedup_key"])) < threshold:
                continue
            if group == "drift":
                affected_ci = str(record.get("ci_id", "cmdb"))
                failure_class = f"cmdb-drift-{record.get('kind', 'unknown')}"
                title = f"CMDB drift: {record.get('kind', 'unknown')} on {affected_ci}"
                impact = str(record.get("detail", ""))[:500]
                severity = "sev2" if material else "sev3"
            else:
                target = str(record.get("target", "unknown"))
                affected_ci = make_ci_id(CIType.HOST.value, target)
                failure_class = f"cmdb-scan-{record.get('collector', 'unknown')}"
                title = f"CMDB collection repeatedly failed on {target}"
                impact = str(record.get("failure", ""))[:500]
                severity = "sev3"
            existing = mgr.find_open_incident_for_service(affected_ci)
            incident = existing or mgr.create_incident(
                title,
                severity=severity,
                source="service_health",
                affected_services=[affected_ci],
                impact=impact,
                managed_by=config.agent,
                created_by=config.agent,
                tags=["cmdb", "reconciliation", group],
                failure_class=failure_class,
            )
            if incident is not None:
                incident_ids.append(incident.id)
    return sorted(set(incident_ids))


def prune_run_artifacts(home: Path, retention_runs: int) -> list[Path]:
    """Remove the oldest run artifact pairs beyond the configured retention."""
    if retention_runs < 1:
        raise ValueError("retention_runs must be positive")
    directory = Path(home).expanduser() / "cmdb" / "reconcile-runs"
    paths = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in paths[retention_runs:]:
        checksum = path.with_suffix(".sha256")
        path.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        removed.append(path)
    if removed:
        receipt = directory.parent / "reconcile-retention-last.json"
        atomic_write_text(
            receipt,
            json.dumps(
                {"retention_runs": retention_runs, "removed": [path.name for path in removed]},
                sort_keys=True,
            )
            + "\n",
        )
    return removed
