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
# Live verdicts are qualified, e.g. BLOCKED_FAIL_CLOSED,
# BLOCKED_ACCURATE_OUTCOME_NOT_RUNTIME_APPROVAL. Exact equality against "BLOCKED"
# matches none of them, so match on the family.
_BLOCKED_RE = re.compile(r"^BLOCKED\b|^BLOCKED_", re.IGNORECASE)
# Evidence keys carrying an outcome. The evidence store spells these many ways
# (verdict has 41 spellings, review_decision several more), so fold before matching.
_OUTCOME_KEY_RE = re.compile(r"(^|_)(verdict|result|disposition|review_decision)(_|$)")
# Ephemeral fleet workers are named pi-auto-<card>/pi-<slug>. Named agents such as
# jarvis or lumina hold claims deliberately and must never be treated as dead.
_EPHEMERAL_OWNER_RE = re.compile(r"^(pi|codex)[-_]", re.IGNORECASE)


def _fold_key(key: object) -> str:
    """Fold a link_key to a comparable form. Mirrors schemas/evidence_vocab.py."""
    k = str(key or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)
    k = re.sub(r"__+", "_", k).strip("_")
    return k


def load_evidence(evidence_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load the SEPARATE evidence store, keyed by card_id.

    Structure lives in cards/<id>/events/*.jsonl; evidence lives in
    coordination/card_events/*.jsonl as link events. Neither store alone answers
    whether a card is done AND passed. Reading only structure is why a card
    claimed for 96 hours with a recorded BLOCKED verdict was reported as having
    no stale claims at all.
    """
    rows: dict[str, list[dict[str, Any]]] = {}
    if not evidence_dir or not Path(evidence_dir).is_dir():
        return rows
    for path in sorted(Path(evidence_dir).glob("*.jsonl")):
        for event in _json_object_lines(path):
            if event.get("action") != "link":
                continue
            card_id = event.get("card_id")
            if not isinstance(card_id, str):
                continue
            rows.setdefault(card_id, []).append(
                {
                    "ts": event.get("ts"),
                    "key": _fold_key(event.get("link_key")),
                    "raw_key": event.get("link_key"),
                    "value": event.get("link_value"),
                    "event_id": event.get("event_id") or event.get("link_key"),
                }
            )
    for card_id in rows:
        rows[card_id].sort(
            key=lambda r: (str(r.get("ts") or ""), str(r.get("event_id") or ""))
        )
    return rows


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


def _json_object_lines(
    path: Path, damaged: list[dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Parse one JSONL event file, skipping lines that are not yet whole.

    ~/.skcapstone is a single Syncthing folder, so an append made on one host can
    be observed mid-flight on another as a truncated final line. Raising on that
    aborts the whole assessment, which aborts the whole rotation, on every host at
    once. A partial line is skipped and recorded instead.
    """
    rows: list[dict[str, Any]] = []
    try:
        source_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        if damaged is not None:
            damaged.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        return rows
    for number, line in enumerate(source_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            if damaged is not None:
                damaged.append(
                    {
                        "path": f"{path}:{number}",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            continue
        if not isinstance(value, dict):
            if damaged is not None:
                damaged.append(
                    {"path": f"{path}:{number}", "error": "line is not a JSON object"}
                )
            continue
        rows.append(value)
    return rows


def load_cards(
    cards_dir: Path, unreadable: list[dict[str, str]] | None = None
) -> dict[str, CardRecord]:
    """Parse every core and every event line before classification.

    A card whose core cannot be parsed is ISOLATED, not fatal. It is recorded in
    ``unreadable`` and left out of the records, so the caller can exclude it from
    assignment while every other card is still assessed.

    This function used to raise. Because ~/.skcapstone is one Syncthing folder,
    a core.json written on one host is visible mid-write on the others, and a
    single partial file aborted the assessment on EVERY host simultaneously. The
    rotation exits non-zero when the assessment fails, so one transient truncated
    write stopped the entire fleet until it happened to be re-read intact.
    Measured 2026-08-27: all five hosts logged
    "lifecycle reassessment failed: Expecting property name enclosed in double
    quotes: line 2 column 1 (char 2)" and launched nothing.
    """
    records: dict[str, CardRecord] = {}
    for card_dir in sorted(cards_dir.iterdir() if cards_dir.is_dir() else ()):
        core_path = card_dir / "core.json"
        if not card_dir.is_dir() or not core_path.is_file():
            continue
        try:
            core = json.loads(core_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            if unreadable is not None:
                unreadable.append(
                    {
                        "card_id": card_dir.name,
                        "path": str(core_path),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            continue
        if not isinstance(core, dict):
            if unreadable is not None:
                unreadable.append(
                    {
                        "card_id": card_dir.name,
                        "path": str(core_path),
                        "error": "core.json is not a JSON object",
                    }
                )
            continue
        events: list[dict[str, Any]] = []
        event_dir = card_dir / "events"
        for event_path in sorted(
            event_dir.glob("*.jsonl") if event_dir.is_dir() else ()
        ):
            events.extend(_json_object_lines(event_path, unreadable))
        events.sort(
            key=lambda row: (
                str(row.get("ts", "")),
                str(row.get("writer", "")),
                row.get("seq", 0),
            )
        )
        card_id = str(core.get("id") or card_dir.name)
        records[card_id] = CardRecord(card_id, core, tuple(events))
    return records


def _actions(record: CardRecord) -> list[str]:
    return [str(event.get("action")) for event in record.events]


def _dependencies(record: CardRecord) -> list[str]:
    dependencies = [
        str(value) for value in record.core.get("dependencies", []) if value
    ]
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
        if (
            action == "claim"
            and isinstance(event.get("owner"), str)
            and event.get("owner")
        ):
            claim = event
        elif action in {"complete", "unassign", "release_claim", "void", "archive"}:
            claim = None
    return claim


def _blocked_evidence_after(
    record: CardRecord,
    after: datetime,
    evidence_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Find a BLOCKED outcome recorded after `after`, joining BOTH stores.

    Previously this read only record.events, the structure store. Verdicts are
    written to the evidence store as link events, so the detector never saw one
    in production and reported stale_claims: 0 while cards sat claimed for 96
    hours with recorded BLOCKED verdicts and approving review decisions.
    """
    result = None

    # structure store: some writers embed the verdict directly on an event
    for event in record.events:
        event_time = _parse_time(event.get("ts"))
        verdict = event.get("verdict")
        if (
            event_time
            and event_time >= after
            and isinstance(verdict, str)
            and _BLOCKED_RE.match(verdict)
        ):
            artifact_hash = (
                event.get("artifact_sha256")
                or event.get("verdict_hash")
                or event.get("evidence_sha256")
            )
            if isinstance(artifact_hash, str) and len(artifact_hash) == 64:
                result = event

    # evidence store: the normal case
    for row in evidence_rows or ():
        row_time = _parse_time(row.get("ts"))
        if not row_time or row_time < after:
            continue
        if not _OUTCOME_KEY_RE.search(row.get("key", "")):
            continue
        value = row.get("value")
        if isinstance(value, str) and _BLOCKED_RE.match(value):
            result = {
                "event_id": row.get("event_id"),
                "ts": row.get("ts"),
                "verdict": value,
                "source": "evidence_store",
                "link_key": row.get("raw_key"),
            }
    return result


def _superseded_ids(
    records: dict[str, CardRecord],
    evidence: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[str]]:
    """Map each superseded card to the cards that replace it.

    This used to read only the structure store: core labels, title and
    description, and events whose action is "supersede". Supersession is
    normally recorded the other way, as a link in the EVIDENCE store, and those
    were invisible here. It is the same two-store defect already documented
    above for BLOCKED verdicts, which was fixed in _blocked_evidence_after and
    never applied to this function.

    Measured on the live board 2026-08-27: c91a7504 carries superseded_by
    2209f7fe and disposition superseded-unclaimable-use-2209f7fe, recorded
    2026-08-23T07:39:47. It was never classed superseded, never excluded, and
    stayed assignable. Only blocked_backoff kept a worker off it, and backoff
    lifts on any material change, so the card was one label away from being
    handed out again in place of its own successor.
    """
    successors: dict[str, list[str]] = {}
    for record in records.values():
        candidates: set[str] = set()
        for label in record.core.get("initial_labels", []):
            if isinstance(label, str) and label.lower().startswith("supersedes-"):
                candidates.add(label[len("supersedes-") :])
        text = " ".join(
            (str(record.core.get("title", "")), str(record.core.get("description", "")))
        )
        candidates.update(match.group(1) for match in _SUPERSEDES_RE.finditer(text))
        for event in record.events:
            if event.get("action") == "supersede" and isinstance(
                event.get("supersedes"), str
            ):
                candidates.add(event["supersedes"])
        for old_id in candidates:
            if old_id in records and old_id != record.card_id:
                successors.setdefault(old_id, []).append(record.card_id)

    # evidence store: the normal case, and the one that was missing.
    for card_id, rows in (evidence or {}).items():
        for row in rows:
            key = str(row.get("key") or row.get("raw_key") or "").strip().lower()
            value = str(row.get("value") or "").strip()
            if not value:
                continue
            if key == "superseded_by":
                old, new = card_id, value
            elif key == "supersedes":
                old, new = value, card_id
            else:
                continue
            # A link may name a full id or a short prefix. Resolve it, and never
            # let an ambiguous prefix silently retire the wrong card.
            if new not in records:
                matches = [c for c in records if c.startswith(new)]
                if len(matches) != 1:
                    continue
                new = matches[0]
            if old not in records:
                matches = [c for c in records if c.startswith(old)]
                if len(matches) != 1:
                    continue
                old = matches[0]
            # A HUMAN gate is never retired by a link in the evidence store.
            # superseded_by there is free-form: any worker can write it, and
            # honouring it would let a machine card discharge an approval only a
            # person can give. Measured on this board, doing so would have
            # retired 36afc5e8 and 64fbb530 in favour of the machine task
            # 6dd21df9, and retired the open gate fcb4147f. Structure-store
            # supersession above is a deliberate authored act and still counts;
            # this path is evidence, and evidence does not carry that authority.
            old_core = records[old].core
            old_title = str(old_core.get("title", "")).upper()
            old_labels = {
                str(x).lower() for x in (old_core.get("initial_labels") or [])
            }
            if "[HUMAN]" in old_title or "human-gate" in old_labels:
                continue
            if old != new:
                successors.setdefault(old, []).append(new)
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
    evidence_dir: Path | None = None,
    dead_worker_after: timedelta = timedelta(hours=6),
) -> dict[str, Any]:
    """Return a deterministic, mutation free lifecycle report and exclusion set."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unreadable_rows: list[dict[str, str]] = []
    records = load_cards(cards_dir, unreadable_rows)
    actions = {card_id: _actions(record) for card_id, record in records.items()}
    if evidence_dir is None:
        evidence_dir = Path(cards_dir).parent / "coordination" / "card_events"
    evidence = load_evidence(Path(evidence_dir))

    void_edges: list[dict[str, str]] = []
    for dependent in records.values():
        for dependency in _dependencies(dependent):
            if dependency in records and "void" in actions[dependency]:
                void_edges.append(
                    {"card_id": dependent.card_id, "void_dependency_id": dependency}
                )

    stale_claims: list[dict[str, Any]] = []
    dead_worker_claims: list[dict[str, Any]] = []
    for record in records.values():
        claim = _current_claim(record)
        claimed_at = _parse_time(claim.get("ts")) if claim else None
        if not claim or not claimed_at or now - claimed_at < stale_after:
            continue
        card_evidence = evidence.get(record.card_id, [])
        blocked = _blocked_evidence_after(record, claimed_at, card_evidence)
        age_hours = int((now - claimed_at).total_seconds() // 3600)
        if blocked:
            # class 2a: finished in evidence, structure never caught up.
            stale_claims.append(
                {
                    "card_id": record.card_id,
                    "owner": claim["owner"],
                    "claimed_at": claimed_at.isoformat(),
                    "age_hours": age_hours,
                    "evidence_event_id": blocked.get("event_id"),
                    "verdict": blocked.get("verdict"),
                    "evidence_source": blocked.get("source", "structure_store"),
                    "kind": "finished_in_evidence",
                }
            )
            continue
        # class 2b: no evidence at all since the claim, and the owner is an
        # ephemeral worker that cannot still be alive. A named agent (jarvis,
        # lumina) or a human may hold a claim deliberately and is never included.
        owner = claim.get("owner") or ""
        produced_anything = any(
            (_parse_time(row.get("ts")) or claimed_at) >= claimed_at
            for row in card_evidence
        )
        if (
            not produced_anything
            and _EPHEMERAL_OWNER_RE.match(str(owner))
            and now - claimed_at >= dead_worker_after
        ):
            dead_worker_claims.append(
                {
                    "card_id": record.card_id,
                    "owner": owner,
                    "claimed_at": claimed_at.isoformat(),
                    "age_hours": age_hours,
                    "kind": "dead_worker",
                }
            )

    launch_counts = _launch_counts(launch_log_roots)
    unclaimable: list[dict[str, Any]] = []
    for card_id, launches in sorted(launch_counts.items()):
        record = records.get(card_id)
        if launches >= 2 and (record is None or "claim" not in actions[card_id]):
            unclaimable.append(
                {
                    "card_id": card_id,
                    "launch_count": launches,
                    "reason": "repeated_launch_without_claim",
                }
            )

    superseded = _superseded_ids(records, evidence)
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
        elif record.core.get("kind") == "incident" and _VOLATILE_CI_RE.search(
            description
        ):
            volatile_ci.append(
                {"card_id": record.card_id, "reason": "volatile_ci_identity"}
            )

    classes: dict[str, list[dict[str, Any]]] = {
        "void_dependency_edges": sorted(
            void_edges, key=lambda row: (row["card_id"], row["void_dependency_id"])
        ),
        "stale_claims": sorted(stale_claims, key=lambda row: row["card_id"]),
        "dead_worker_claims": sorted(
            dead_worker_claims, key=lambda row: row["card_id"]
        ),
        "unclaimable_cards": unclaimable,
        "superseded_cards": superseded_rows,
        "volatile_ci_identity": sorted(volatile_ci, key=lambda row: row["card_id"]),
        "unreadable_cards": sorted(
            (row for row in unreadable_rows if row.get("card_id")),
            key=lambda row: (row.get("card_id", ""), row.get("path", "")),
        ),
    }
    excluded = sorted(
        {
            row["card_id"]
            for name in (
                "void_dependency_edges",
                "stale_claims",
                "dead_worker_claims",
                "unclaimable_cards",
                "superseded_cards",
                "unreadable_cards",
            )
            for row in classes[name]
        }
    )
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
            "dead_worker_claims": "Release the claim. Reversible, asserts no outcome; the card returns to the pool.",
            "unclaimable_cards": "Close or normalize the source record in a separate approved action.",
            "superseded_cards": "Append a canonical supersession event in a separate approved action.",
            "volatile_ci_identity": "Complete CMDB-IDENT-01 before incident reconciliation.",
            "unreadable_cards": "Excluded from assignment, not fatal. A card here is usually a "
            "transient partial write seen across the Syncthing folder and clears on the next "
            "pass. One that persists is genuinely corrupt and needs the core rewritten from "
            "its event log.",
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
