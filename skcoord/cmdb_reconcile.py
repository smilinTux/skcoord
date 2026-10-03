"""Bounded, auditable orchestration for CMDB discovery and reconciliation.

This module deliberately has no scheduler integration.  Callers can shadow it,
inspect its immutable run artifact, and only then decide whether to replace a
timer.  A failed or partial scan is data about collector health, never evidence
that an asset disappeared.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .atomic_io import atomic_write_text
from .cmdb import CIStatus, CMDBManager, is_secret_attribute_key
from .discovery import (
    DECLARED_COLLECTORS,
    OBSERVED_COLLECTORS,
    CommandRunner,
    DiscoveredCI,
    DriftFinding,
    ReconcileReport,
    drift,
    merge,
    reconcile,
)

_SCHEMA = "skcoord.cmdb.reconcile-run/v1"
def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Target:
    """A scan target and the authority that put it in scope."""

    host: str
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class CollectorCoverage:
    """Non-secret command/result accounting for one collector invocation."""

    collector: str
    commands_attempted: int
    commands_succeeded: int
    commands_unavailable: int
    findings: int
    status: str


@dataclass
class TargetResult:
    """Bounded collector result for one target."""

    host: str
    provenance: tuple[str, ...]
    expected_collectors: int
    completed_collectors: int = 0
    findings: list[DiscoveredCI] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    coverage: list[CollectorCoverage] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return (
            self.completed_collectors == self.expected_collectors
            and not self.failures
            and not any(record.status == "unavailable" for record in self.coverage)
        )


@dataclass
class ScanResult:
    """Discovery output plus the evidence needed to judge completeness."""

    discovered: list[DiscoveredCI]
    targets: list[TargetResult]
    declared_failures: list[str] = field(default_factory=list)
    deadline_exceeded: bool = False
    failure_budget_exceeded: bool = False
    collector_scope: tuple[str, ...] = ()
    config_scope: str = ""

    @property
    def complete(self) -> bool:
        return (
            not self.deadline_exceeded
            and not self.failure_budget_exceeded
            and not self.declared_failures
            and bool(self.targets)
            and all(result.complete for result in self.targets)
        )

    def completeness(self) -> dict:
        expected = sum(item.expected_collectors for item in self.targets)
        completed = sum(item.completed_collectors for item in self.targets)
        coverage = [record for item in self.targets for record in item.coverage]
        return {
            "complete": self.complete,
            "targets_expected": len(self.targets),
            "targets_complete": sum(item.complete for item in self.targets),
            "collectors_expected": expected,
            "collectors_complete": completed,
            "collectors_partial": sum(record.status == "partial" for record in coverage),
            "collectors_unavailable": sum(
                record.status == "unavailable" for record in coverage
            ),
            "deadline_exceeded": self.deadline_exceeded,
            "failure_budget_exceeded": self.failure_budget_exceeded,
        }

    def scope_fingerprint(self) -> str:
        """Bind lifecycle evidence to the exact target/collector scope."""
        scope = {
            "targets": [
                {
                    "host": item.host,
                    "provenance": list(item.provenance),
                    "expected_collectors": item.expected_collectors,
                }
                for item in sorted(self.targets, key=lambda result: result.host)
            ],
            "collectors": list(self.collector_scope),
            "config": self.config_scope,
        }
        return hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class OrchestrationConfig:
    """Resource and failure bounds for one whole-network scan."""

    global_concurrency: int = 8
    per_host_concurrency: int = 2
    deadline_seconds: float = 300.0
    failure_budget: int = 0

    def __post_init__(self) -> None:
        if min(self.global_concurrency, self.per_host_concurrency) < 1:
            raise ValueError("concurrency limits must be positive")
        if self.deadline_seconds <= 0 or self.failure_budget < 0:
            raise ValueError("deadline must be positive and failure_budget non-negative")


@dataclass
class _CoverageRunner:
    """Record result availability without retaining commands or output."""

    inner: CommandRunner
    commands_attempted: int = 0
    commands_succeeded: int = 0
    commands_unavailable: int = 0

    @property
    def host(self) -> str:
        return self.inner.host

    def run(self, argv: Sequence[str]) -> str | None:
        self.commands_attempted += 1
        result = self.inner.run(argv)
        if result is None:
            self.commands_unavailable += 1
        else:
            self.commands_succeeded += 1
        return result

    def record(self, collector: str, findings: int) -> CollectorCoverage:
        if self.commands_succeeded == 0:
            status = "unavailable"
        elif self.commands_unavailable:
            status = "partial"
        else:
            status = "complete"
        return CollectorCoverage(
            collector,
            self.commands_attempted,
            self.commands_succeeded,
            self.commands_unavailable,
            findings,
            status,
        )


def resolve_targets(
    home: Path, approved: Mapping[str, Sequence[str]] | None = None
) -> list[Target]:
    """Build a deduplicated target set from fleet nodes and approved sources.

    ``approved`` is intentionally explicit: keys name a reviewed source and
    values are hostnames. Discovery output itself can never expand scan scope.
    """
    sources: dict[str, set[str]] = {}
    root = Path(home).expanduser() / "fleet" / "objects" / "node"
    for path in sorted(root.glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        address = (obj.get("spec") or {}).get("address") or {}
        host = address.get("hostname") or obj.get("name") or path.stem
        if host:
            sources.setdefault(str(host), set()).add(f"fleet:node:{path.stem}")
    for source, hosts in sorted((approved or {}).items()):
        for host in hosts:
            if str(host).strip():
                sources.setdefault(str(host).strip(), set()).add(f"approved:{source}")
    return [
        Target(host, tuple(sorted(provenance))) for host, provenance in sorted(sources.items())
    ]


def scan_network(
    home: Path,
    targets: Sequence[Target],
    runner_factory: Callable[[str], CommandRunner],
    config: OrchestrationConfig = OrchestrationConfig(),
) -> ScanResult:
    """Run declared and observed collectors with global/per-host bounds."""
    found: list[DiscoveredCI] = []
    scan_id = uuid.uuid4().hex
    observed_at = _now().isoformat()
    declared_failures: list[str] = []
    for collector in DECLARED_COLLECTORS:
        try:
            found.extend(collector(Path(home)))
        except Exception as exc:  # noqa: BLE001
            declared_failures.append(f"{collector.__name__}:{type(exc).__name__}")

    results = {
        target.host: TargetResult(target.host, target.provenance, len(OBSERVED_COLLECTORS))
        for target in targets
    }
    semaphores = {
        target.host: threading.BoundedSemaphore(config.per_host_concurrency) for target in targets
    }
    started = time.monotonic()

    def invoke(
        host: str, observer: Callable[[CommandRunner], list[DiscoveredCI]]
    ) -> tuple[str, str, list[DiscoveredCI], str, CollectorCoverage | None]:
        with semaphores[host]:
            try:
                runner = runner_factory(host)
                if runner.run(["true"]) is None:
                    return host, observer.__name__, [], "transport_unavailable", None
                covered = _CoverageRunner(runner)
                items = [
                    replace(
                        item,
                        observed_at=item.observed_at or observed_at,
                        scan_id=item.scan_id or scan_id,
                        authority=item.authority or f"network:{host}",
                    )
                    for item in observer(covered)
                ]
                return host, observer.__name__, items, "", covered.record(
                    observer.__name__, len(items)
                )
            except Exception as exc:  # noqa: BLE001
                return host, observer.__name__, [], type(exc).__name__, None

    pool = ThreadPoolExecutor(
        max_workers=config.global_concurrency, thread_name_prefix="cmdb-scan"
    )
    futures: dict[Future, tuple[str, str]] = {}
    for target in targets:
        for observer in OBSERVED_COLLECTORS:
            futures[pool.submit(invoke, target.host, observer)] = (target.host, observer.__name__)
    deadline_exceeded = False
    try:
        remaining = max(0.0, config.deadline_seconds - (time.monotonic() - started))
        for future in as_completed(futures, timeout=remaining):
            host, collector, items, failure, coverage = future.result()
            result = results[host]
            result.completed_collectors += 1
            result.findings.extend(items)
            if coverage is not None:
                result.coverage.append(coverage)
            if failure:
                result.failures.append(f"{collector}:{failure}")
    except FuturesTimeoutError:
        deadline_exceeded = True
        for future, (host, collector) in futures.items():
            if not future.done():
                future.cancel()
                results[host].failures.append(f"{collector}:deadline")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for result in results.values():
        result.duration_seconds = round(time.monotonic() - started, 6)
        result.coverage.sort(key=lambda record: record.collector)
        found.extend(result.findings)
    failures = len(declared_failures) + sum(len(item.failures) for item in results.values())
    return ScanResult(
        merge(found),
        list(results.values()),
        declared_failures,
        deadline_exceeded,
        failures > config.failure_budget,
        tuple(
            sorted(
                f"{collector.__module__}.{collector.__qualname__}"
                for collector in (*DECLARED_COLLECTORS, *OBSERVED_COLLECTORS)
            )
        ),
        hashlib.sha256(
            json.dumps(asdict(config), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


@dataclass(frozen=True)
class NormalizedDrift:
    """Stable operator/event representation of actionable asset drift."""

    event_type: str
    dedup_key: str
    ci_id: str
    kind: str
    severity: str
    detail: str
    scan_id: str
    scan_complete: bool
    evidence: str


@dataclass(frozen=True)
class ScanHealthEvent:
    """Collector failure signal, deliberately separate from asset drift."""

    event_type: str
    dedup_key: str
    scan_id: str
    target: str
    collector: str
    failure: str
    evidence: str


def normalize_drift(
    findings: Sequence[DriftFinding], scan_id: str, complete: bool, evidence: str
) -> list[NormalizedDrift]:
    """Normalize and deduplicate drift; suppress absence claims on partial scans."""
    absence = {"declared_not_observed", "stored_not_discovered"}
    deduped: dict[str, NormalizedDrift] = {}
    for finding in findings:
        if not complete and finding.kind in absence:
            continue
        severity = "high" if finding.kind == "declared_not_observed" else "medium"
        key = hashlib.sha256(
            f"{finding.ci_id}\0{finding.kind}\0{finding.detail}".encode()
        ).hexdigest()
        deduped[key] = NormalizedDrift(
            "cmdb.drift",
            key,
            finding.ci_id,
            finding.kind,
            severity,
            finding.detail[:500],
            scan_id,
            complete,
            evidence,
        )
    return sorted(deduped.values(), key=lambda item: (item.severity, item.ci_id, item.kind))


def scan_health_events(result: ScanResult, scan_id: str, evidence: str) -> list[ScanHealthEvent]:
    """Translate collector failures into a normalized health-event seam."""
    failures: list[tuple[str, str, str]] = []
    failures.extend(
        ("declared", item.partition(":")[0], item) for item in result.declared_failures
    )
    for target in result.targets:
        for item in target.failures:
            collector, _, failure = item.partition(":")
            failures.append((target.host, collector, failure or item))
    events = {}
    for target, collector, failure in failures:
        key = hashlib.sha256(f"{target}\0{collector}\0{failure}".encode()).hexdigest()
        events[key] = ScanHealthEvent(
            "cmdb.scan_health", key, scan_id, target, collector, failure, evidence
        )
    return sorted(events.values(), key=lambda item: (item.target, item.collector, item.failure))


def apply_retirement_lifecycle(
    mgr: CMDBManager,
    authority: str,
    scope_fingerprint: str,
    discovered_ids: Sequence[str],
    owned_ids: Sequence[str],
    complete: bool,
    threshold: int = 3,
    apply: bool = False,
    agent: str = "cmdb-lifecycle",
) -> list[dict]:
    """Advance per-authority misses and retire after N complete comparable passes."""
    if threshold < 1:
        raise ValueError("threshold must be positive")
    if not authority.strip() or not re.fullmatch(r"[0-9a-f]{64}", scope_fingerprint):
        raise ValueError("authority and a SHA-256 scope_fingerprint are required")
    if not complete:
        return []
    seen, actions = set(discovered_ids), []
    key = f"discovery_misses:{authority}:{scope_fingerprint}"
    for ci_id in sorted(set(owned_ids)):
        ci = mgr.get_ci(ci_id)
        if ci is None or "discovered" not in ci.tags:
            continue
        previous = int(ci.attributes.get(key, 0) or 0)
        misses = 0 if ci_id in seen else previous + 1
        action = "reset" if ci_id in seen and previous else "miss" if ci_id not in seen else "seen"
        dependents = (
            mgr.impact_analysis(ci_id).get("dependents", []) if misses >= threshold else []
        )
        retire = misses >= threshold and ci.status != CIStatus.RETIRED.value
        actions.append(
            {
                "ci_id": ci_id,
                "authority": authority,
                "action": "retire" if retire else action,
                "misses": misses,
                "threshold": threshold,
                "dependents": dependents,
            }
        )
        if apply and misses != previous:
            mgr.set_attribute(ci_id, agent, key, misses)
        if apply and retire:
            mgr.set_status(
                ci_id,
                agent,
                CIStatus.RETIRED.value,
                note=f"not seen in {threshold} complete {authority} scans",
            )
    return actions


def _sanitize_with_findings(
    value: object, path: str = "$"
) -> tuple[object, list[dict[str, str]]]:
    """Redact secret-named fields and return their paths without their values."""
    findings: list[dict[str, str]] = []
    if isinstance(value, dict):
        clean = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if key == "secret_redaction_findings":
                clean[key], nested = _sanitize_with_findings(child, child_path)
                findings.extend(nested)
            elif is_secret_attribute_key(key):
                clean[key] = "[redacted]"
                findings.append({"path": child_path})
            else:
                clean[key], nested = _sanitize_with_findings(child, child_path)
                findings.extend(nested)
        return clean, findings
    if isinstance(value, (list, tuple)):
        clean_items = []
        for index, item in enumerate(value[:1000]):
            clean, nested = _sanitize_with_findings(item, f"{path}[{index}]")
            clean_items.append(clean)
            findings.extend(nested)
        return clean_items, findings
    if isinstance(value, str):
        return value[:1000], findings
    return value, findings


def _sanitize(value: object) -> object:
    """Compatibility wrapper for callers that only need the clean value."""
    return _sanitize_with_findings(value)[0]


def write_run_artifact(home: Path, artifact: dict) -> tuple[Path, str]:
    """Write a canonical, secret-safe run artifact and return its checksum."""
    clean = _sanitize({**artifact, "schema": _SCHEMA})
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":")) + "\n"
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    run_id = str(clean.get("scan_id") or uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
        raise ValueError("scan_id is not safe for an artifact filename")
    directory = Path(home).expanduser() / "cmdb" / "reconcile-runs"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f"{run_id}.json"
    atomic_write_text(path, payload)
    atomic_write_text(path.with_suffix(".sha256"), f"{checksum}  {path.name}\n")
    return path, checksum


def read_verified_run_artifacts(home: Path) -> list[dict]:
    """Read checksum-valid reconcile artifacts, newest first, without writing."""
    directory = Path(home).expanduser() / "cmdb" / "reconcile-runs"
    verified: list[tuple[float, dict]] = []
    for path in directory.glob("*.json"):
        try:
            payload = path.read_bytes()
            expected = path.with_suffix(".sha256").read_text(encoding="utf-8").split()[0]
            if hashlib.sha256(payload).hexdigest() != expected:
                continue
            value = json.loads(payload)
            if isinstance(value, dict):
                verified.append((path.stat().st_mtime, value))
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return [value for _, value in sorted(verified, key=lambda item: item[0], reverse=True)]


def freshness_status(last_success: datetime | None, now: datetime, slo: timedelta) -> dict:
    """Evaluate the testable freshness primitive used by timer/source alerts."""
    age = None if last_success is None else max(0.0, (now - last_success).total_seconds())
    return {
        "fresh": age is not None and age <= slo.total_seconds(),
        "age_seconds": age,
        "slo_seconds": slo.total_seconds(),
    }


def operator_summary(artifacts: Sequence[dict], now: datetime, slo: timedelta) -> dict:
    """Build the small stable read model used by CLI/dashboard surfaces."""
    ordered = sorted(artifacts, key=lambda item: item.get("ended_at", ""), reverse=True)
    latest = ordered[0] if ordered else None
    successes = [item for item in ordered if item.get("completeness", {}).get("complete")]
    last_success = None
    if successes:
        try:
            last_success = datetime.fromisoformat(successes[0]["ended_at"])
        except (KeyError, TypeError, ValueError):
            last_success = None
    drift_summary = (latest or {}).get("drift", {"count": 0, "by_severity": {}})
    return {
        "latest_scan_id": (latest or {}).get("scan_id"),
        "latest_complete": bool((latest or {}).get("completeness", {}).get("complete")),
        "latest_drift": drift_summary,
        "freshness": freshness_status(last_success, now, slo),
        "recent_failed_or_partial": sum(
            not item.get("completeness", {}).get("complete", False) for item in ordered[:10]
        ),
    }


def run_reconcile(
    mgr: CMDBManager,
    scan_result: ScanResult,
    apply: bool = False,
    code_version: str = "unknown",
    config_version: str = "unknown",
    lifecycle_actions: Sequence[dict] = (),
    agent: str = "cmdb-discovery",
) -> tuple[dict, list[NormalizedDrift]]:
    """Reconcile one scan and construct its durable operator-facing record."""
    scan_id, started = str(uuid.uuid4()), _now()
    scope_fingerprint = scan_result.scope_fingerprint()
    scoped_discovered = [
        replace(
            item,
            attributes={**item.attributes, "lifecycle_scope": scope_fingerprint},
        )
        for item in scan_result.discovered
    ]
    report: ReconcileReport = reconcile(
        mgr,
        scoped_discovered,
        agent=agent,
        apply=apply,
        scan_complete=scan_result.complete,
    )
    evidence = f"cmdb/reconcile-runs/{scan_id}.json"
    normalized = normalize_drift(
        drift(scoped_discovered, mgr), scan_id, scan_result.complete, evidence
    )
    ended = _now()
    retired = sorted(item["ci_id"] for item in lifecycle_actions if item.get("action") == "retire")
    report_data = report.as_dict()
    report_data["retired"] = retired
    report_data["counts"]["retired"] = len(retired)
    scan_validation_failures = [
        {"scope": "declared", "reason": failure} for failure in scan_result.declared_failures
    ]
    scan_validation_failures.extend(
        {"scope": target.host, "reason": failure}
        for target in scan_result.targets
        for failure in target.failures
    )
    stale_candidates = sorted(
        (
            item
            for item in lifecycle_actions
            if item.get("action") in {"miss", "retire"}
        ),
        key=lambda item: item.get("ci_id", ""),
    )
    artifact = {
        "scan_id": scan_id,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": (ended - started).total_seconds(),
        "applied": apply,
        "code_version": code_version,
        "config_version": config_version,
        "scope_fingerprint": scope_fingerprint,
        "completeness": scan_result.completeness(),
        "reconcile": report_data,
        "plan": {
            "creates": report.created,
            "updates": report.updated,
            "relationships": report.relationships,
            "stale_candidates": stale_candidates,
            "retirements": retired,
            "validation_failures": [
                *report.validation_failures,
                *scan_validation_failures,
            ],
            "secret_redaction_findings": report.secret_redaction_findings,
        },
        "drift": {
            "count": len(normalized),
            "by_severity": {
                severity: sum(item.severity == severity for item in normalized)
                for severity in ("high", "medium", "low")
            },
        },
        "collector_health": {
            "event_count": len(scan_health_events(scan_result, scan_id, evidence)),
            "declared_failures": scan_result.declared_failures,
            "targets": [
                {**asdict(item), "findings": len(item.findings), "complete": item.complete}
                for item in scan_result.targets
            ],
        },
        "events": {
            "drift": [asdict(item) for item in normalized],
            "scan_health": [
                asdict(item) for item in scan_health_events(scan_result, scan_id, evidence)
            ],
        },
    }
    clean_artifact, artifact_redactions = _sanitize_with_findings(artifact)
    clean_artifact["plan"]["secret_redaction_findings"] = sorted(
        [*report.secret_redaction_findings, *artifact_redactions],
        key=lambda item: (item.get("ci_id", ""), item.get("path", "")),
    )
    if apply and report.validation_failures:
        clean_artifact["applied"] = False
    return clean_artifact, normalized
