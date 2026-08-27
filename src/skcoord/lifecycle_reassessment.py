"""Read only lifecycle integrity assessment for CardStore rotation inputs."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

_VERDICTS = {"PASS", "PASS_FOR_REVIEW", "BLOCKED"}
_TERMINAL = {"complete", "void", "archive"}
_SUPERSEDES_RE = re.compile(r"\bsupersedes(?:\s+card)?\s+([a-z0-9-]+)", re.IGNORECASE)
_VOLATILE_CI_RE = re.compile(r"(?:^|[-_])\d{2,}(?:[-_][0-9a-f]{6,})?$")


@dataclass(frozen=True)
class CardRecord:
    card_id: str
    core: dict[str, Any]
    events: tuple[dict[str, Any], ...]


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number} must contain a JSON object")
            rows.append(value)
    return rows


def load_cards(cards_dir: Path) -> dict[str, CardRecord]:
    """Parse every core and every event line before classification."""
    records: dict[str, CardRecord] = {}
    for card_dir in sorted(cards_dir.iterdir() if cards_dir.is_dir() else ()):
        core_path = card_dir / "core.json"
        if not card_dir.is_dir() or not core_path.is_file():
            continue
        core = json.loads(core_path.read_text(encoding="utf-8"))
        if not isinstance(core, dict):
            raise ValueError(f"{core_path} must contain a JSON object")
        events: list[dict[str, Any]] = []
        event_dir = card_dir / "events"
        for event_path in sorted(event_dir.glob("*.jsonl") if event_dir.is_dir() else ()):
            events.extend(_json_object_lines(event_path))
        events.sort(key=lambda row: (str(row.get("ts", "")), str(row.get("writer", "")), row.get("seq", 0)))
        card_id = str(core.get("id") or card_dir.name)
        records[card_id] = CardRecord(card_id, core, tuple(events))
    return records


def _actions(record: CardRecord) -> list[str]:
    return [str(event.get("action")) for event in record.events]


def _dependencies(record: CardRecord) -> list[str]:
    dependencies = [str(value) for value in record.core.get("dependencies", []) if value]
    for event in record.events:
        dependency = event.get("dependency")
        if event.get("action") == "add_dependency" and isinstance(dependency, str):
            if dependency not in dependencies:
                dependencies.append(dependency)
        elif event.get("action") == "remove_dependency" and dependency in dependencies:
            dependencies.remove(dependency)
    return dependencies


def _current_claim(record: CardRecord) -> dict[str, Any] | None:
    claim: dict[str, Any] | None = None
    for event in record.events:
        action = event.get("action")
        if action == "claim" and isinstance(event.get("owner"), str) and event.get("owner"):
            claim = event
        elif action in {"complete", "unassign", "release_claim", "void", "archive"}:
            claim = None
    return claim


def _blocked_evidence_after(record: CardRecord, after: datetime) -> dict[str, Any] | None:
    result = None
    for event in record.events:
        event_time = _parse_time(event.get("ts"))
        verdict = event.get("verdict")
        if event_time and event_time >= after and verdict in _VERDICTS and verdict == "BLOCKED":
            artifact_hash = event.get("artifact_sha256") or event.get("verdict_hash") or event.get("evidence_sha256")
            if isinstance(artifact_hash, str) and len(artifact_hash) == 64:
                result = event
    return result


def _superseded_ids(records: dict[str, CardRecord]) -> dict[str, list[str]]:
    successors: dict[str, list[str]] = {}
    for record in records.values():
        candidates: set[str] = set()
        for label in record.core.get("initial_labels", []):
            if isinstance(label, str) and label.lower().startswith("supersedes-"):
                candidates.add(label[len("supersedes-"):])
        text = " ".join((str(record.core.get("title", "")), str(record.core.get("description", ""))))
        candidates.update(match.group(1) for match in _SUPERSEDES_RE.finditer(text))
        for event in record.events:
            if event.get("action") == "supersede" and isinstance(event.get("supersedes"), str):
                candidates.add(event["supersedes"])
        for old_id in candidates:
            if old_id in records and old_id != record.card_id:
                successors.setdefault(old_id, []).append(record.card_id)
    return {card_id: sorted(set(values)) for card_id, values in successors.items()}


def _launch_counts(log_roots: Iterable[Path]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for root in log_roots:
        for path in root.glob("*/actions.log") if root.is_dir() else ():
            with path.open(encoding="utf-8") as source:
                for line in source:
                    parts = line.rstrip("\n").split("|")
                    if len(parts) >= 4 and parts[0] == "LAUNCHED":
                        counts[parts[3]] += 1
    return counts


def assess(
    cards_dir: Path,
    launch_log_roots: Iterable[Path] = (),
    *,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Return a deterministic, mutation free lifecycle report and exclusion set."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = load_cards(cards_dir)
    actions = {card_id: _actions(record) for card_id, record in records.items()}

    void_edges: list[dict[str, str]] = []
    for dependent in records.values():
        for dependency in _dependencies(dependent):
            if dependency in records and "void" in actions[dependency]:
                void_edges.append({"card_id": dependent.card_id, "void_dependency_id": dependency})

    stale_claims: list[dict[str, Any]] = []
    for record in records.values():
        claim = _current_claim(record)
        claimed_at = _parse_time(claim.get("ts")) if claim else None
        if not claim or not claimed_at or now - claimed_at < stale_after:
            continue
        evidence = _blocked_evidence_after(record, claimed_at)
        if evidence:
            stale_claims.append({
                "card_id": record.card_id,
                "owner": claim["owner"],
                "claimed_at": claimed_at.isoformat(),
                "age_hours": int((now - claimed_at).total_seconds() // 3600),
                "evidence_event_id": evidence.get("event_id"),
            })

    launch_counts = _launch_counts(launch_log_roots)
    unclaimable: list[dict[str, Any]] = []
    for card_id, launches in sorted(launch_counts.items()):
        record = records.get(card_id)
        if launches >= 2 and (record is None or "claim" not in actions[card_id]):
            unclaimable.append({"card_id": card_id, "launch_count": launches, "reason": "repeated_launch_without_claim"})

    superseded = _superseded_ids(records)
    superseded_rows = [
        {"card_id": card_id, "superseded_by": successors}
        for card_id, successors in sorted(superseded.items())
    ]

    volatile_ci: list[dict[str, Any]] = []
    for record in records.values():
        title = str(record.core.get("title", ""))
        description = str(record.core.get("description", ""))
        if "CMDB-IDENT-01" in title:
            volatile_ci.append({"card_id": record.card_id, "reason": "tracking_card"})
        elif record.core.get("kind") == "incident" and _VOLATILE_CI_RE.search(description):
            volatile_ci.append({"card_id": record.card_id, "reason": "volatile_ci_identity"})

    classes: dict[str, list[dict[str, Any]]] = {
        "void_dependency_edges": sorted(void_edges, key=lambda row: (row["card_id"], row["void_dependency_id"])),
        "stale_claims": sorted(stale_claims, key=lambda row: row["card_id"]),
        "unclaimable_cards": unclaimable,
        "superseded_cards": superseded_rows,
        "volatile_ci_identity": sorted(volatile_ci, key=lambda row: row["card_id"]),
    }
    excluded = sorted({
        row["card_id"]
        for name in ("void_dependency_edges", "stale_claims", "unclaimable_cards", "superseded_cards")
        for row in classes[name]
    })
    report: dict[str, Any] = {
        "schema": "skcoord.lifecycle-reassessment/v1",
        "generated_at": now.isoformat(),
        "read_only": True,
        "classes": classes,
        "counts": {name: len(rows) for name, rows in classes.items()},
        "excluded_card_ids": excluded,
        "proposed_remediations": {
            "void_dependency_edges": "Approve separate dependency removal or successor edge actions.",
            "stale_claims": "Approve separate claim release and dependent successor actions.",
            "unclaimable_cards": "Close or normalize the source record in a separate approved action.",
            "superseded_cards": "Append a canonical supersession event in a separate approved action.",
            "volatile_ci_identity": "Complete CMDB-IDENT-01 before incident reconciliation.",
        },
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["content_sha256"] = hashlib.sha256(canonical).hexdigest()
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    """Serialize and verify a report before replacing its derived output file."""
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("serialized lifecycle report is not an object")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(output)
