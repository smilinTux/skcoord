"""
SKCapstone ITIL Service Management - Incident, Problem, and Change Management.

Conflict-free design (prb-7810b08e / chg-11d0e1c7): each mutable record is a
directory keyed only by its id.  The immutable birth-facts live in a write-once
``core.json``; every subsequent change is an append-only event line in a
per-writer log (``events/<agent>@<host>.jsonl``).  Current state (status,
severity, timeline, timestamps, resolution) is *folded* deterministically on
read by sorting all events across every writer file and replaying them through
the lifecycle transition tables.  Single-writer-per-file means disjoint write
sets, so Syncthing has nothing to conflict.  CAB votes already used this
per-agent-file pattern; this generalizes it to the whole record set.

Directory layout:
    ~/.skcapstone/coordination/itil/
    ├── incidents/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── problems/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── changes/<id>/{core.json, events/<agent>@<host>.jsonl}
    ├── cab-decisions/<change_id>-<agent>.json   # per-agent CAB vote (unchanged)
    ├── kedb/<id>.json                           # write-once (slug dropped)
    └── ITIL-BOARD.md                            # regenerated on one pinned node
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .atomic_io import atomic_write_text

logger = logging.getLogger("skcapstone.itil")

# Node identifier used to make every writer file globally unique
# (``events/<agent>@<host>.jsonl``).  A bare ``<agent>.jsonl`` is forbidden -
# that is the heartbeat-v1 collision documented in ~/.skcapstone/.stignore.
_HOSTNAME = socket.gethostname()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class IncidentStatus(str, Enum):
    DETECTED = "detected"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CLOSED = "closed"


# The statuses an incident holds while it is still live work. Used both to find
# the open incident for a service and to gate the auto-created GTD item: we only
# mirror an incident into GTD once it has actually persisted as an open record.
OPEN_INCIDENT_STATUSES = frozenset({"detected", "acknowledged", "investigating", "escalated"})


class ProblemStatus(str, Enum):
    IDENTIFIED = "identified"
    ANALYZING = "analyzing"
    KNOWN_ERROR = "known_error"
    RESOLVED = "resolved"


class ChangeType(str, Enum):
    """ITIL change taxonomy for a stored change record.

    NOTE (drift register D4): the operator-seat classifier
    (`operator_seat/policy.py::classify_change`) uses a four-value taxonomy
    (standard / normal / major / emergency) for its risk/authority decision.
    There is deliberately no ``MAJOR`` member here: `major` is an operator-seat
    risk class, not a persisted change type. At the create boundary a `major`
    classification maps to ``ChangeType.NORMAL`` WITHOUT the ``auto-normal`` tag,
    so it falls through `_fold_change` to the CAB derivation and requires a
    ``human`` approval to unblock. That is the "CAB-major" convention: the AI can
    never self-authorize a major change. See
    ITIL_AND_RUNBOOK_OPERATING_MODEL_STANDARD.md section 9 D4.
    """

    STANDARD = "standard"
    NORMAL = "normal"
    EMERGENCY = "emergency"


class ChangeStatus(str, Enum):
    PROPOSED = "proposed"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    REJECTED = "rejected"
    IMPLEMENTING = "implementing"
    DEPLOYED = "deployed"
    VERIFIED = "verified"
    FAILED = "failed"
    CLOSED = "closed"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CABDecisionValue(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    ABSTAIN = "abstain"


# ---------------------------------------------------------------------------
# Lifecycle state machines - valid transitions
# ---------------------------------------------------------------------------

# NOTE (drift register D2): the `resolved -> investigating` reopen edge is
# deliberately NOT a row here. Reopen is a distinct `reopen` event kind that the
# fold (`_fold_incident`) owns, because it does more than a status change: it
# also clears `resolved_at` and `resolution_summary`. Keeping it out of this
# table means a plain `status` event can never silently reopen an incident
# without that side effect. The sk-standards ITIL diagram draws it as a
# fold-only edge; see ITIL_AND_RUNBOOK_OPERATING_MODEL_STANDARD.md section 9 D2.
_INCIDENT_TRANSITIONS: dict[str, set[str]] = {
    "detected": {"acknowledged", "escalated", "resolved"},
    "acknowledged": {"investigating", "escalated", "resolved"},
    "investigating": {"escalated", "resolved"},
    "escalated": {"investigating", "resolved"},
    "resolved": {"closed"},  # reopen (resolved->investigating) is fold-only, see note above
    "closed": set(),
}

_PROBLEM_TRANSITIONS: dict[str, set[str]] = {
    "identified": {"analyzing"},
    "analyzing": {"known_error", "resolved"},
    "known_error": {"resolved"},
    "resolved": set(),
}

_CHANGE_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"reviewing", "approved", "rejected"},
    "reviewing": {"approved", "rejected"},
    # "scheduled" added (CR change-mgmt P1.1): approved -> scheduled via a
    # dedicated `schedule` event (see _fold_change); "approved -> implementing"
    # stays legal untouched so the existing manual-implementer path is unchanged.
    "approved": {"implementing", "rejected", "scheduled"},
    # New row: scheduled -> implementing is the DEPLOY edge (change-deploy-runner,
    # a later phase, appends a plain "status" event); scheduled -> approved is
    # unschedule / a missed window, both dedicated event kinds in _fold_change.
    "scheduled": {"implementing", "approved", "rejected"},
    "rejected": {"closed"},
    "implementing": {"deployed", "failed"},
    "deployed": {"verified", "failed"},
    "verified": {"closed"},
    "failed": {"implementing", "closed"},
    "closed": set(),
}

# Severity rank - higher is more severe.  Folded severity takes the max
# (escalate-only), which is safer for alerting and reproduces the historical
# escalate-only behavior across concurrent writers.
_SEV_RANK: dict[str, int] = {"sev1": 4, "sev2": 3, "sev3": 2, "sev4": 1}


def _max_severity(a: str, b: str) -> str:
    """Return the more severe of two severity strings (sev1 wins)."""
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TimelineEntry(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    agent: str
    action: str
    note: str = ""


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"inc-{uuid.uuid4().hex[:8]}")
    type: str = "incident"
    title: str
    severity: Severity = Severity.SEV3
    status: IncidentStatus = IncidentStatus.DETECTED
    source: str = "manual"
    affected_services: list[str] = Field(default_factory=list)
    impact: str = ""
    managed_by: str = ""
    created_by: str = ""
    detected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None
    closed_at: Optional[str] = None
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    related_problem_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
    resolution_summary: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class Problem(BaseModel):
    id: str = Field(default_factory=lambda: f"prb-{uuid.uuid4().hex[:8]}")
    type: str = "problem"
    title: str
    status: ProblemStatus = ProblemStatus.IDENTIFIED
    root_cause: Optional[str] = None
    workaround: Optional[str] = None
    managed_by: str = ""
    created_by: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    related_incident_ids: list[str] = Field(default_factory=list)
    related_change_id: Optional[str] = None
    kedb_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Change(BaseModel):
    id: str = Field(default_factory=lambda: f"chg-{uuid.uuid4().hex[:8]}")
    type: str = "change"
    title: str
    change_type: ChangeType = ChangeType.NORMAL
    status: ChangeStatus = ChangeStatus.PROPOSED
    risk: Risk = Risk.MEDIUM
    rollback_plan: str = ""
    test_plan: str = ""
    managed_by: str = ""
    created_by: str = ""
    implementer: Optional[str] = None
    cab_required: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    related_problem_id: Optional[str] = None
    gtd_item_ids: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # Change-mgmt P1.1 additions (CAB + AI prepare/deploy/scheduling design,
    # docs/specs/2026-08-13-change-management-cab-ai-arch.md section 4.2).
    # All optional / default None: a change record whose event log carries
    # none of the five new event kinds (pr_link/validation/schedule/
    # unschedule/window_missed) folds byte-identically to before this card.
    prepared_pr: Optional[dict[str, Any]] = None
    prepared_by: Optional[str] = None
    validation: Optional[dict[str, Any]] = None
    scheduled_window: Optional[dict[str, Any]] = None


class KEDBEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"ke-{uuid.uuid4().hex[:8]}")
    title: str
    symptoms: list[str] = Field(default_factory=list)
    root_cause: str = ""
    workaround: str = ""
    permanent_fix_change_id: Optional[str] = None
    related_problem_id: Optional[str] = None
    managed_by: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags: list[str] = Field(default_factory=list)


class CABDecision(BaseModel):
    change_id: str
    agent: str
    subject_role: str = ""
    subject_fingerprint: str = ""
    authorization_id: str = ""
    decision: CABDecisionValue = CABDecisionValue.ABSTAIN
    conditions: str = ""
    decided_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _is_human_approval(vote: CABDecision) -> bool:
    """Return whether a vote carries a qualifying human approval role."""
    return vote.agent == "human" or vote.subject_role in {"owner", "approver"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug (kept for the human ``label``)."""
    slug = text.lower().strip()
    slug = re.sub(r'[/\\:*?"<>|]', "-", slug)
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")[:40]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_timeline_entry(agent: str, action: str, note: str = "") -> dict[str, str]:
    return {
        "ts": _now_iso(),
        "agent": agent,
        "action": action,
        "note": note,
    }


def _auto_incident_id(service: str, failure_class: str, day_bucket: str) -> str:
    """Compute a deterministic incident id for an auto-detected outage.

    Two nodes detecting the same failure of the same service on the same day
    compute the identical id, so they converge on one ``core.json``.

    Args:
        service: Affected service name (e.g. ``"skvector (Qdrant)"``).
        failure_class: Coarse failure classification (e.g. ``"unreachable"``).
        day_bucket: ``YYYY-MM-DD`` window key.

    Returns:
        A stable ``inc-<hash>`` id.
    """
    key = f"{service}|{failure_class}|{day_bucket}"
    return "inc-" + hashlib.blake2b(key.encode("utf-8"), digest_size=4).hexdigest()


def _cab_resolved_status(
    status: str,
    change_type: str,
    tags: list[str],
    core: dict,
    votes: list["CABDecision"],
    prepared_by: Optional[str],
) -> str:
    """Return the CAB/standard/auto-normal fold-time derivation of *status*.

    ``_fold_change`` applies this exact derivation once, after its main event
    loop, to produce the record's headline status (never stored, pure
    read-time). Some events - the pre-existing generic ``status`` kind, and
    the new ``schedule``/``unschedule``/``window_missed`` kinds (change-mgmt
    P1.1) - need to validate a transition *against* that derived state
    mid-loop: e.g. can a change move to "implementing" or "scheduled" when
    its "approved" status only ever existed as a fold-time derivation, never
    an explicit ``status`` event? Before this helper existed, it could not -
    the loop's own ``status`` local stayed "proposed" for any change whose
    approval came purely from CAB votes or a standard/auto-normal
    auto-approve, so a subsequent explicit event (even the pre-existing,
    untouched "approved -> implementing" manual path) always folded as
    conflicted. This closes that pre-existing gap; it is read-only /
    advisory and never appends timeline entries itself - the post-loop block
    in ``_fold_change`` still owns the record's own approval timeline entry
    and its exact wording, duplicating these same conditions (kept in sync
    by hand; see that block's comments).

    No-self-approval (change-mgmt P1.4): an APPROVE vote whose voter equals
    *prepared_by* is excluded here exactly as it is in the post-loop block,
    so a drafter's self-approval can never gate a mid-fold transition either.

    Args:
        status: The loop's current status at this point in the replay.
        change_type: The change's ``ChangeType`` value.
        tags: Tags accumulated by the loop so far.
        core: The change's immutable birth-facts dict.
        votes: All CAB votes for this change (order-independent).
        prepared_by: The drafter identity from the latest ``pr_link`` event
            processed so far in the loop, or ``None``.

    Returns:
        ``status`` unchanged if it is not "proposed"/"reviewing", or if no
        derivation currently applies; otherwise the derived "approved" or
        "rejected".
    """
    if status not in ("proposed", "reviewing"):
        return status
    if change_type == "standard":
        return "approved"
    if (
        change_type == "normal"
        and "auto-normal" in tags
        and core.get("created_by") == "operator"
        and core.get("risk", "medium") != "high"
        and (core.get("rollback_plan") or "").strip()
        and not any(v.decision == CABDecisionValue.REJECTED for v in votes)
    ):
        return "approved"
    rejections = [v for v in votes if v.decision == CABDecisionValue.REJECTED]
    if rejections:
        return "rejected"
    approvals = [
        v for v in votes if v.decision == CABDecisionValue.APPROVED and v.agent != prepared_by
    ]
    if any(_is_human_approval(v) for v in approvals):
        return "approved"
    return status


# ---------------------------------------------------------------------------
# ITILManager
# ---------------------------------------------------------------------------


class ITILManager:
    """Manages ITIL records on disk as immutable core + folded event logs.

    Args:
        home: Path to the shared root (``~/.skcapstone`` or equivalent).
    """

    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.itil_dir = self.home / "coordination" / "itil"
        self.incidents_dir = self.itil_dir / "incidents"
        self.problems_dir = self.itil_dir / "problems"
        self.changes_dir = self.itil_dir / "changes"
        self.kedb_dir = self.itil_dir / "kedb"
        self.cab_dir = self.itil_dir / "cab-decisions"

    def ensure_dirs(self) -> None:
        """Create ITIL directories if they don't exist."""
        for d in (
            self.incidents_dir,
            self.problems_dir,
            self.changes_dir,
            self.kedb_dir,
            self.cab_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # ── File I/O: immutable core + append-only per-writer event logs ──────

    def _writer_id(self, agent: str) -> str:
        """Return this process's globally-unique writer key ``<agent>@<host>``."""
        return f"{agent or 'unknown'}@{_HOSTNAME}"

    def _write_core(self, directory: Path, record_id: str, core: dict) -> Path:
        """Write ``<dir>/<id>/core.json`` write-once, create-if-absent.

        Uses ``O_CREAT|O_EXCL`` so a concurrent create race on the same
        deterministic id is safe: the loser gets ``FileExistsError`` and the
        existing (byte-identical, for deterministic ids) core wins.

        Args:
            directory: The record-type directory (incidents/problems/changes).
            record_id: Stable record id (the directory name - never a slug).
            core: The immutable birth-facts dict.

        Returns:
            Path to ``core.json`` (existing one on a create race).
        """
        self.ensure_dirs()
        rec_dir = directory / record_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        core_path = rec_dir / "core.json"
        payload = (json.dumps(core, indent=2, default=str) + "\n").encode("utf-8")
        try:
            fd = os.open(str(core_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return core_path
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return core_path

    def _append_event(
        self, directory: Path, record_id: str, agent: str, kind: str, **payload: Any
    ) -> None:
        """Append one event line to this writer's own log (flock-guarded).

        ``seq`` is assigned as the current line count of the writer's file, so
        it monotonically increases per-writer and tie-breaks equal timestamps
        from the same writer.

        Args:
            directory: The record-type directory.
            record_id: Stable record id.
            agent: The logical writer (agent) name.
            kind: Event kind (``status``/``severity``/``note``/...).
            **payload: Kind-specific fields (``to``, ``note``, ``id``, ...).
        """
        rec_dir = directory / record_id
        events_dir = rec_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{self._writer_id(agent)}.jsonl"
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                seq = sum(1 for _ in fh)
                event = {
                    "event_id": uuid.uuid4().hex,
                    "ts": _now_iso(),
                    "writer": agent,
                    "node": _HOSTNAME,
                    "seq": seq,
                    "kind": kind,
                }
                event.update(payload)
                fh.seek(0, os.SEEK_END)
                fh.write(json.dumps(event, default=str) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_events(self, directory: Path, record_id: str) -> list[dict]:
        """Read + totally-order every event across all writer files.

        The sort key ``(ts, node, writer, seq)`` is present in the data and
        identical on every replica once Syncthing converges, giving a
        deterministic CRDT-style op-log order with no locking.
        """
        events: list[dict] = []
        events_dir = directory / record_id / "events"
        if not events_dir.is_dir():
            return events
        for f in sorted(events_dir.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed event line in %s", f.name)
                    continue
        events.sort(
            key=lambda e: (
                e.get("ts", ""),
                e.get("node", ""),
                e.get("writer", ""),
                e.get("seq", 0),
            )
        )
        return events

    def _load_core(self, directory: Path, record_id: str) -> Optional[dict]:
        """Load a record's immutable ``core.json`` (or None if absent/bad)."""
        core_path = directory / record_id / "core.json"
        if not core_path.exists():
            return None
        try:
            return json.loads(core_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Bad core.json for %s: %s", record_id, exc)
            return None

    def _resolve_id(self, directory: Path, record_id: str) -> str:
        """Follow ``redirect.json`` stubs (from migration merges) to canonical id."""
        seen: set[str] = set()
        cur = record_id
        while cur not in seen:
            seen.add(cur)
            rec_dir = directory / cur
            if (rec_dir / "core.json").exists():
                return cur
            redirect = rec_dir / "redirect.json"
            if not redirect.exists():
                return cur
            try:
                cur = json.loads(redirect.read_text(encoding="utf-8"))["canonical"]
            except Exception:  # noqa: BLE001
                return cur
        return cur

    def _record_exists(self, directory: Path, record_id: str) -> bool:
        """True if the (redirect-resolved) record has a ``core.json``."""
        rid = self._resolve_id(directory, record_id)
        return (directory / rid / "core.json").exists()

    def _writer_has_kind(self, directory: Path, record_id: str, agent: str, kind: str) -> bool:
        """True if *agent*'s own writer file already holds an event of *kind*.

        Reads only this writer's own file (cheap, no sync lag).  Replaces the
        old fragile last-3-timeline-notes host-tag guard with a structural,
        own-file check that bounds recovery-note volume to one edge per host.
        """
        path = directory / record_id / "events" / f"{self._writer_id(agent)}.jsonl"
        if not path.exists():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("kind") == kind:
                        return True
                except json.JSONDecodeError:
                    continue
        except OSError:
            return False
        return False

    def _load_records(self, directory: Path, model_class: type) -> list:
        """Fold every record directory under *directory* into models.

        Keeps skip-and-warn tolerance for legacy flat ``<id>-<slug>.json``
        files so an old/new mixed tree during migration never crashes a read.
        """
        records: list = []
        if not directory.exists():
            return records
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir():
                if entry.suffix == ".json":
                    logger.warning(
                        "Skipping legacy flat ITIL file %s - run itil_migrate_events.py",
                        entry.name,
                    )
                continue
            # Bulk load only real records. A redirect-only directory (a merge
            # or re-key stub with no core.json) is a pointer for direct
            # lookups, not a record of its own - skipping it avoids counting
            # the canonical record twice.
            if not (entry / "core.json").exists():
                continue
            rec = self._fold_record(directory, entry.name, model_class)
            if rec is not None:
                records.append(rec)
        return records

    def _fold_record(self, directory: Path, record_id: str, model_class: type):
        """Load core + events for one record and return the folded model."""
        rec_dir = directory / record_id
        core = self._load_core(directory, record_id)
        if core is None:
            redirect = rec_dir / "redirect.json"
            if redirect.exists():
                try:
                    target = json.loads(redirect.read_text(encoding="utf-8"))["canonical"]
                    if target != record_id:
                        return self._fold_record(directory, target, model_class)
                except Exception:  # noqa: BLE001
                    return None
            return None
        events = self._read_events(directory, record_id)
        try:
            if model_class is Incident:
                return self._fold_incident(core, events)
            if model_class is Problem:
                return self._fold_problem(core, events)
            if model_class is Change:
                votes = self.get_cab_votes(core.get("id", record_id))
                return self._fold_change(core, events, votes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fold failed for %s: %s", record_id, exc)
            return None
        return None

    # ── Fold algorithms (pure derivation of state from the event log) ─────

    def _fold_incident(self, core: dict, events: list[dict]) -> Incident:
        """Fold an incident's event log into a fully-populated Incident."""
        status = "detected"
        severity = core.get("severity_at_creation") or "sev3"
        timeline: list[dict[str, Any]] = []
        acknowledged_at = resolved_at = closed_at = None
        resolution_summary = None
        related_problem_id = core.get("related_problem_id")
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        seen_created = False

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind == "created":
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "created", "note": note})
            elif kind == "status":
                to = e.get("to")
                if to in _INCIDENT_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                    if to == "acknowledged" and not acknowledged_at:
                        acknowledged_at = ts
                    elif to == "resolved":
                        if not resolved_at:
                            resolved_at = ts
                        if e.get("resolution_summary"):
                            resolution_summary = e["resolution_summary"]
                    elif to == "closed" and not closed_at:
                        closed_at = ts
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "reopen":
                if status == "resolved":
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": "status:resolved->investigating",
                            "note": note or "reopened",
                        }
                    )
                    status = "investigating"
                    resolved_at = None
                    resolution_summary = None
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"reopen:{status}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "severity":
                to = e.get("to")
                timeline.append(
                    {
                        "ts": ts,
                        "agent": agent,
                        "action": f"severity:{severity}->{to}",
                        "note": note,
                    }
                )
                severity = _max_severity(severity, to)
            elif kind == "ack":
                if not acknowledged_at:
                    acknowledged_at = ts
                timeline.append({"ts": ts, "agent": agent, "action": "acknowledged", "note": note})
            elif kind == "resolution":
                if e.get("resolution_summary"):
                    resolution_summary = e["resolution_summary"]
            elif kind == "link_problem":
                if e.get("id"):
                    related_problem_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind in ("note", "recovery"):
                timeline.append({"ts": ts, "agent": agent, "action": "note", "note": note})
            # any other kind (gtd_complete, etc.) is timeline-silent

        return Incident(
            id=core["id"],
            type="incident",
            title=title,
            severity=Severity(severity),
            status=IncidentStatus(status),
            source=core.get("source", "manual"),
            affected_services=list(core.get("affected_services") or []),
            impact=core.get("impact", ""),
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            detected_at=core.get("detected_at") or _now_iso(),
            acknowledged_at=acknowledged_at,
            resolved_at=resolved_at,
            closed_at=closed_at,
            timeline=timeline,
            related_problem_id=related_problem_id,
            gtd_item_ids=gtd_ids,
            resolution_summary=resolution_summary,
            tags=tags,
        )

    def _fold_problem(self, core: dict, events: list[dict]) -> Problem:
        """Fold a problem's event log into a fully-populated Problem."""
        status = "identified"
        timeline: list[dict[str, Any]] = []
        root_cause = core.get("root_cause")
        workaround = core.get("workaround")
        kedb_id = core.get("kedb_id")
        related_change_id = core.get("related_change_id")
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        seen_created = False

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind == "created":
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "created", "note": note})
            elif kind == "status":
                to = e.get("to")
                if to in _PROBLEM_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "root_cause":
                if e.get("text"):
                    root_cause = e["text"]
            elif kind == "workaround":
                if e.get("text"):
                    workaround = e["text"]
            elif kind == "link_kedb":
                if e.get("id"):
                    kedb_id = e["id"]
            elif kind == "link_change":
                if e.get("id"):
                    related_change_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind == "note":
                timeline.append({"ts": ts, "agent": agent, "action": "note", "note": note})

        return Problem(
            id=core["id"],
            type="problem",
            title=title,
            status=ProblemStatus(status),
            root_cause=root_cause,
            workaround=workaround,
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            created_at=core.get("created_at") or _now_iso(),
            related_incident_ids=list(core.get("related_incident_ids") or []),
            related_change_id=related_change_id,
            kedb_id=kedb_id,
            gtd_item_ids=gtd_ids,
            timeline=timeline,
            tags=tags,
        )

    def _fold_change(self, core: dict, events: list[dict], votes: list["CABDecision"]) -> Change:
        """Fold a change's event log + CAB votes into a Change.

        Standard-change auto-approval and CAB approval/rejection are pure
        fold-time derivations (no writer ever mutates the change record for
        them), reproducing the old ``_evaluate_cab`` logic exactly.
        """
        change_type = core.get("change_type", "normal")
        status = "proposed"
        timeline: list[dict[str, Any]] = []
        tags = list(core.get("tags") or [])
        gtd_ids: list[str] = []
        title = core.get("title", "")
        related_problem_id = core.get("related_problem_id")
        seen_created = False
        # Change-mgmt P1.1: event-only fields, never seeded from core (a
        # change is never created with a draft PR / verdict / schedule
        # already attached), so they start unset and stay unset for every
        # pre-existing record whose event log carries none of the five new
        # kinds below - the fold-invariance guarantee.
        prepared_pr: Optional[dict[str, Any]] = None
        prepared_by: Optional[str] = None
        validation: Optional[dict[str, Any]] = None
        scheduled_window: Optional[dict[str, Any]] = None

        for e in events:
            kind = e.get("kind")
            note = e.get("note", "") or ""
            agent = e.get("writer", "") or ""
            ts = e.get("ts", "")
            if kind in ("created", "proposed"):
                if seen_created:
                    continue
                seen_created = True
                timeline.append({"ts": ts, "agent": agent, "action": "proposed", "note": note})
            elif kind == "status":
                to = e.get("to")
                # gate_status resolves a CAB/standard/auto-normal derivation
                # that already promoted this change past proposed/reviewing
                # even though no explicit `status` event ever recorded it -
                # closes a pre-existing gap where e.g. the untouched
                # "approved -> implementing" manual path silently conflicted
                # for any change approved purely by CAB vote (see
                # _cab_resolved_status docstring).
                gate_status = _cab_resolved_status(
                    status, change_type, tags, core, votes, prepared_by
                )
                # CM P3.3 PIR lifecycle guard (design doc section 3, "deployed
                # -> verified: post-implementation review"): two edges demand
                # a non-empty note on the SAME status event, or they fold as
                # a conflict entry, fail-closed, same treatment as an invalid
                # transition. Neither edge is a new row in _CHANGE_TRANSITIONS
                # (both already existed pre-P3.3); this only narrows when an
                # otherwise-legal transition is accepted.
                #   - deployed -> verified needs a PIR / smoke-check note.
                #   - failed -> closed needs a rollback note.
                pir_gated = gate_status == "deployed" and to == "verified"
                rollback_gated = gate_status == "failed" and to == "closed"
                # CAB bypass guard: a raw `status` event may never be the thing
                # that grants approval. `_cab_resolved_status` above already
                # derives "approved" for every LEGITIMATE route (a qualifying
                # CAB vote, with the drafter's self-approval excluded, plus the
                # standard/auto-normal auto-approve), so if the gate still
                # reads proposed/reviewing here then no such route applied and
                # this event is trying to self-promote. Without this, any
                # caller of `update_change(..., new_status="approved")` -- the
                # MCP tool and CLI included -- could approve its own change
                # with a free-text agent string and no vote, silently routing
                # around submit_cab_vote() and its no-self-approval fold guard.
                # EXEMPTION - historical replay: `itil_migrate_events.py` maps a
                # legacy "status:proposed->approved" timeline entry onto this
                # same `status` kind, stamping node="migrated" (_MIGRATED_NODE).
                # Those approvals were granted under the pre-event-sourcing
                # regime and their vote records no longer exist to re-derive, so
                # re-gating them would silently demote every migrated change
                # back to proposed. Live events always carry the real hostname
                # (verified), and forging this marker requires direct write
                # access to the event JSONL, which is already game-over.
                cab_gated = (
                    to == "approved"
                    and gate_status in ("proposed", "reviewing")
                    and e.get("node") != "migrated"
                )
                if (
                    to in _CHANGE_TRANSITIONS.get(gate_status, set())
                    and not ((pir_gated or rollback_gated) and not note.strip())
                    and not cab_gated
                ):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{gate_status}->{to}",
                            "note": note,
                        }
                    )
                    status = to
                elif cab_gated:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{gate_status}->{to}",
                            "note": note,
                            "conflicted": True,
                            "conflict_reason": (
                                "CAB approval required: approval comes from a "
                                "qualifying vote (submit_cab_vote) or a "
                                "standard/auto-normal derivation, never from a "
                                "raw status event"
                            ),
                        }
                    )
                elif (pir_gated or rollback_gated) and not note.strip():
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{gate_status}->{to}",
                            "note": note,
                            "conflicted": True,
                            "conflict_reason": (
                                "PIR note required" if pir_gated else "rollback note required"
                            ),
                        }
                    )
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{gate_status}->{to}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "link_problem":
                if e.get("id"):
                    related_problem_id = e["id"]
            elif kind == "title":
                if e.get("text"):
                    title = e["text"]
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "title", "note": e.get("text", "")}
                )
            elif kind == "tags":
                for t in e.get("add") or []:
                    if t not in tags:
                        tags.append(t)
            elif kind == "gtd_link":
                gid = e.get("id")
                if gid and gid not in gtd_ids:
                    gtd_ids.append(gid)
            elif kind in ("note", "auto-approved"):
                action = "auto-approved" if kind == "auto-approved" else "note"
                timeline.append({"ts": ts, "agent": agent, "action": action, "note": note})
            elif kind == "pr_link":
                # Appended by the AI runner when a PREPARE run on this change
                # finishes with a draft PR. Last-write-wins on re-prepare;
                # `prepared_by` is the writer, never a claimed field on the
                # payload, so it cannot be spoofed by event content.
                prepared_pr = {
                    "url": e.get("url"),
                    "branch": e.get("branch"),
                    "run_id": e.get("run_id"),
                    "head_sha": e.get("head_sha"),
                }
                prepared_by = agent
                timeline.append(
                    {"ts": ts, "agent": agent, "action": "pr_link", "note": e.get("url", "")}
                )
            elif kind == "validation":
                # CI verdict attached to the draft PR. Latest event wins. A
                # PASS while still `proposed` is ready-for-CAB: fold-append
                # the `reviewing` transition, mirroring how `reopen` performs
                # a direct status move for incidents.
                validation = {
                    "passed": bool(e.get("passed")),
                    "head_sha": e.get("head_sha"),
                    "url": e.get("url"),
                    "summary": e.get("summary"),
                    "checks": e.get("checks"),
                }
                timeline.append(
                    {
                        "ts": ts,
                        "agent": agent,
                        "action": "validation",
                        "note": "passed" if validation["passed"] else "failed",
                    }
                )
                if validation["passed"] and status == "proposed":
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": "status:proposed->reviewing",
                            "note": "validation passed",
                        }
                    )
                    status = "reviewing"
            elif kind == "schedule":
                # Valid only while approved (same fail-closed treatment as an
                # invalid `status` event: recorded conflicted, no transition).
                # A re-schedule is unschedule + schedule again, so this stays
                # a single, table-driven edge. Uses gate_status (see the
                # `status` kind above) so a change approved purely by CAB
                # vote / standard-auto can be scheduled without ever having
                # carried an explicit "status -> approved" event.
                to = "scheduled"
                gate_status = _cab_resolved_status(
                    status, change_type, tags, core, votes, prepared_by
                )
                if gate_status == "approved" and to in _CHANGE_TRANSITIONS.get(gate_status, set()):
                    scheduled_window = {
                        "window_start": e.get("window_start"),
                        "window_end": e.get("window_end"),
                        "asap": bool(e.get("asap", False)),
                        "deploy_mode": e.get("deploy_mode") or "confirm",
                    }
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{gate_status}->{to}",
                            "note": note or ("ASAP" if scheduled_window["asap"] else ""),
                        }
                    )
                    status = to
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"schedule:{gate_status}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "unschedule":
                # scheduled -> approved, operator-initiated. Clears the window
                # so a subsequent `schedule` event is a clean re-schedule.
                to = "approved"
                if status == "scheduled" and to in _CHANGE_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note or "unscheduled",
                        }
                    )
                    status = to
                    scheduled_window = None
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"unschedule:{status}",
                            "note": note,
                            "conflicted": True,
                        }
                    )
            elif kind == "window_missed":
                # Appended by the deploy runner when now > window_end without
                # a deploy. Fold has no clock of its own - it trusts the event
                # exists in the log at all; the runner is what must never
                # append this late (that invariant lives in the runner, a
                # later phase, not here). Same scheduled -> approved edge as
                # unschedule, with the miss called out distinctly on the
                # timeline and it demands an explicit re-schedule.
                to = "approved"
                if status == "scheduled" and to in _CHANGE_TRANSITIONS.get(status, set()):
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"status:{status}->{to}",
                            "note": note or "window missed",
                        }
                    )
                    status = to
                    scheduled_window = None
                else:
                    timeline.append(
                        {
                            "ts": ts,
                            "agent": agent,
                            "action": f"window_missed:{status}",
                            "note": note,
                            "conflicted": True,
                        }
                    )

        # Standard changes auto-approve at fold time (never stored).
        if change_type == "standard" and status == "proposed":
            status = "approved"
            timeline.append(
                {
                    "ts": core.get("created_at") or _now_iso(),
                    "agent": core.get("created_by", ""),
                    "action": "auto-approved",
                    "note": "Standard change",
                }
            )

        # Auto-normal tier (Operator Seat): a NORMAL change the operator authored
        # may auto-approve at fold ONLY when it is not high-risk, carries a rollback
        # plan, is tagged `auto-normal`, and has no rejection vote. The standing
        # human veto is preserved (a single rejection in the CAB block below still
        # blocks). MAJOR changes are deliberately untouched: they fall through to
        # the CAB derivation, which requires a `human` approval, so the AI can never
        # self-authorize a major change.
        if (
            change_type == "normal"
            and status == "proposed"
            and "auto-normal" in tags
            and core.get("created_by") == "operator"
            and core.get("risk", "medium") != "high"
            and (core.get("rollback_plan") or "").strip()
            and not any(v.decision == CABDecisionValue.REJECTED for v in votes)
        ):
            status = "approved"
            timeline.append(
                {
                    "ts": core.get("created_at") or _now_iso(),
                    "agent": "operator",
                    "action": "auto-approved",
                    "note": "Auto-normal (operator, risk!=high, rollback, no reject)",
                }
            )

        # CAB derivation - mirrors the old _evaluate_cab guard exactly:
        # any rejection blocks; else >=1 human approval unblocks.
        #
        # No-self-approval (CR change-mgmt P1.4, CRITICAL): an APPROVE vote
        # whose voter identity equals `prepared_by` (the drafter of this
        # change's PR) is dropped from the approvals pool before the "any
        # human approval" check runs, regardless of caller - this holds even
        # if a future PEP layer's identity binding is bypassed or buggy. The
        # same drafter's REJECT vote is left in `rejections` untouched: a
        # drafter's veto is always safe and must still block. When
        # `prepared_by` is None (no `pr_link` event, i.e. every change
        # record that predates this card) the filter is a no-op, preserving
        # the fold-invariance guarantee.
        if status in ("proposed", "reviewing") and votes:
            rejections = [v for v in votes if v.decision == CABDecisionValue.REJECTED]
            approvals = [
                v
                for v in votes
                if v.decision == CABDecisionValue.APPROVED and v.agent != prepared_by
            ]
            if rejections:
                status = "rejected"
                timeline.append(
                    {
                        "ts": max(v.decided_at for v in rejections),
                        "agent": "cab-system",
                        "action": "status:proposed->rejected",
                        "note": "Rejected by: " + ", ".join(v.agent for v in rejections),
                    }
                )
            elif any(_is_human_approval(v) for v in approvals):
                status = "approved"
                timeline.append(
                    {
                        "ts": max(v.decided_at for v in approvals),
                        "agent": "cab-system",
                        "action": "status:proposed->approved",
                        "note": "Approved by: " + ", ".join(v.agent for v in approvals),
                    }
                )

        return Change(
            id=core["id"],
            type="change",
            title=title,
            change_type=ChangeType(change_type),
            status=ChangeStatus(status),
            risk=Risk(core.get("risk", "medium")),
            rollback_plan=core.get("rollback_plan", ""),
            test_plan=core.get("test_plan", ""),
            managed_by=core.get("managed_by", ""),
            created_by=core.get("created_by", ""),
            implementer=core.get("implementer"),
            cab_required=bool(core.get("cab_required", change_type != "standard")),
            created_at=core.get("created_at") or _now_iso(),
            related_problem_id=related_problem_id,
            gtd_item_ids=gtd_ids,
            timeline=timeline,
            tags=tags,
            prepared_pr=prepared_pr,
            prepared_by=prepared_by,
            validation=validation,
            scheduled_window=scheduled_window,
        )

    # ── Incidents ─────────────────────────────────────────────────────

    def create_incident(
        self,
        title: str,
        severity: str = "sev3",
        source: str = "manual",
        affected_services: list[str] | None = None,
        impact: str = "",
        managed_by: str = "",
        created_by: str = "",
        tags: list[str] | None = None,
        failure_class: str | None = None,
    ) -> Incident:
        """Create a new incident record.

        Auto-detected incidents (``source == "service_health"``) get a
        deterministic id (``_auto_incident_id``) so two nodes converge on one
        record; all other sources keep a random id.  ``failure_class`` is an
        optional hint (used only for the deterministic id) - the public
        callers (MCP/CLI) never pass it, so their call sites are unchanged.

        Args:
            failure_class: Coarse failure class for the deterministic id
                (default ``"unreachable"`` for service_health).
        """
        agent = managed_by or created_by or "unknown"
        services = affected_services or []
        if source == "service_health":
            svc = services[0] if services else title
            fc = failure_class or "unreachable"
            day_bucket = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            record_id = _auto_incident_id(svc, fc, day_bucket)
            dedup_key = f"{svc}:{fc}"
        else:
            record_id = f"inc-{uuid.uuid4().hex[:8]}"
            dedup_key = None

        detected_at = _now_iso()
        core = {
            "id": record_id,
            "type": "incident",
            "title": title,
            "label": _slugify(title),
            "severity_at_creation": Severity(severity).value,
            "source": source,
            "affected_services": services,
            "impact": impact,
            "managed_by": agent,
            "created_by": created_by or agent,
            "detected_at": detected_at,
            "tags": tags or [],
        }
        if dedup_key:
            core["dedup_key"] = dedup_key

        self._write_core(self.incidents_dir, record_id, core)
        self._append_event(
            self.incidents_dir,
            record_id,
            agent,
            "created",
            note=f"Incident detected: {title}",
        )

        # Auto-create GTD item and link it via an event (no whole-file rewrite).
        # Guard first: make sure an OPEN incident actually persisted (folds back
        # from disk as a live record) before mirroring it into GTD. Without this,
        # a core that failed to persist or diverged across nodes (the recurring
        # ITIL orphan storm) still left a dangling ``[ITIL:...]`` GTD item that the
        # daily validator had to batch-close. No open incident -> no GTD item.
        inc = self._fold_record(self.incidents_dir, record_id, Incident)
        if inc is None or inc.status.value not in OPEN_INCIDENT_STATUSES:
            logger.warning(
                "Skipping GTD emit for %s: incident did not persist as an open record "
                "(status=%s); not creating an orphan next-action.",
                record_id,
                getattr(getattr(inc, "status", None), "value", None),
            )
        else:
            gtd_id = self._create_gtd_item_for_incident(inc)
            if gtd_id:
                self._append_event(self.incidents_dir, record_id, agent, "gtd_link", id=gtd_id)

        self._publish_event(
            "itil.incident.created",
            {
                "id": record_id,
                "title": title,
                "severity": Severity(severity).value,
                "managed_by": agent,
            },
        )

        return self._fold_record(self.incidents_dir, record_id, Incident)

    def update_incident(
        self,
        incident_id: str,
        agent: str,
        new_status: str | None = None,
        severity: str | None = None,
        note: str = "",
        resolution_summary: str | None = None,
        related_problem_id: str | None = None,
    ) -> Incident:
        """Append one event per non-None argument, then return the folded state.

        Transition validation now lives in the fold: a losing concurrent
        transition is flagged ``conflicted`` in the timeline and excluded from
        state, so this method no longer raises on an invalid transition (it
        still raises ``ValueError`` when the incident does not exist).
        """
        rid = self._resolve_id(self.incidents_dir, incident_id)
        if not self._record_exists(self.incidents_dir, rid):
            raise ValueError(f"Incident {incident_id} not found")

        if new_status:
            payload: dict[str, Any] = {"to": new_status, "note": note}
            if new_status == "resolved" and resolution_summary:
                payload["resolution_summary"] = resolution_summary
            self._append_event(self.incidents_dir, rid, agent, "status", **payload)
            if new_status == "resolved":
                folded = self._fold_record(self.incidents_dir, rid, Incident)
                self._complete_gtd_items(folded.gtd_item_ids)

        if severity:
            self._append_event(self.incidents_dir, rid, agent, "severity", to=severity, note=note)
            self._publish_event(
                "itil.incident.escalated",
                {"id": rid, "new_severity": severity},
            )

        if related_problem_id:
            self._append_event(
                self.incidents_dir, rid, agent, "link_problem", id=related_problem_id
            )

        if note and not new_status and not severity:
            self._append_event(self.incidents_dir, rid, agent, "note", note=note)

        return self._fold_record(self.incidents_dir, rid, Incident)

    def list_incidents(
        self,
        status: str | None = None,
        severity: str | None = None,
        service: str | None = None,
    ) -> list[Incident]:
        """List incidents with optional filters."""
        incidents = self._load_records(self.incidents_dir, Incident)
        if status:
            incidents = [i for i in incidents if i.status.value == status]
        if severity:
            incidents = [i for i in incidents if i.severity.value == severity]
        if service:
            incidents = [i for i in incidents if service in i.affected_services]
        return incidents

    def find_open_incident_for_service(self, service: str) -> Optional[Incident]:
        """Find an existing open incident for a service (convenience dedup read).

        No longer the create authority: create-if-absent on the deterministic
        id is.  This remains a cheap convenience so a single node does not
        re-emit a ``created`` event every health cycle while a service is down.
        """
        for inc in self.list_incidents():
            if inc.status.value in OPEN_INCIDENT_STATUSES and service in inc.affected_services:
                return inc
        return None

    def note_recovery(self, incident_id: str, agent: str, note: str) -> None:
        """Append a one-per-host recovery note to an open incident.

        Structurally bounded: appends at most one ``recovery`` event per writer
        (own-file check), so a service flapping up cannot balloon the log.
        """
        rid = self._resolve_id(self.incidents_dir, incident_id)
        if not self._record_exists(self.incidents_dir, rid):
            return
        if self._writer_has_kind(self.incidents_dir, rid, agent, "recovery"):
            return
        self._append_event(self.incidents_dir, rid, agent, "recovery", note=note)

    # ── Problems ──────────────────────────────────────────────────────

    def create_problem(
        self,
        title: str,
        managed_by: str = "",
        created_by: str = "",
        related_incident_ids: list[str] | None = None,
        workaround: str = "",
        tags: list[str] | None = None,
    ) -> Problem:
        """Create a new problem record."""
        agent = managed_by or created_by or "unknown"
        record_id = f"prb-{uuid.uuid4().hex[:8]}"
        core = {
            "id": record_id,
            "type": "problem",
            "title": title,
            "label": _slugify(title),
            "managed_by": agent,
            "created_by": created_by or agent,
            "created_at": _now_iso(),
            "related_incident_ids": related_incident_ids or [],
            "workaround": workaround or None,
            "tags": tags or [],
        }
        self._write_core(self.problems_dir, record_id, core)
        self._append_event(
            self.problems_dir,
            record_id,
            agent,
            "created",
            note=f"Problem identified: {title}",
        )

        # Guard first: same belt-and-suspenders as create_incident (45bb088) -
        # only mirror the problem into GTD once it has actually persisted as a
        # live record. A None fold (unpersisted / diverged core) or an
        # already-resolved fold must not spawn a fresh investigation project.
        prb = self._fold_record(self.problems_dir, record_id, Problem)
        if prb is None or prb.status.value == "resolved":
            logger.warning(
                "Skipping GTD emit for %s: problem did not persist as a live record "
                "(status=%s); not creating an orphan project.",
                record_id,
                getattr(getattr(prb, "status", None), "value", None),
            )
        else:
            gtd_id = self._create_gtd_project_for_problem(prb)
            if gtd_id:
                self._append_event(self.problems_dir, record_id, agent, "gtd_link", id=gtd_id)

        self._publish_event(
            "itil.problem.created",
            {
                "id": record_id,
                "title": title,
                "related_incidents": related_incident_ids or [],
            },
        )

        return self._fold_record(self.problems_dir, record_id, Problem)

    def update_problem(
        self,
        problem_id: str,
        agent: str,
        new_status: str | None = None,
        root_cause: str | None = None,
        workaround: str | None = None,
        note: str = "",
        create_kedb: bool = False,
    ) -> Problem:
        """Append problem events, optionally spawn a KEDB entry, return folded."""
        rid = self._resolve_id(self.problems_dir, problem_id)
        if not self._record_exists(self.problems_dir, rid):
            raise ValueError(f"Problem {problem_id} not found")

        if new_status:
            self._append_event(self.problems_dir, rid, agent, "status", to=new_status, note=note)
            if new_status == ProblemStatus.RESOLVED.value:
                folded = self._fold_record(self.problems_dir, rid, Problem)
                self._complete_gtd_items(folded.gtd_item_ids)

        if root_cause:
            self._append_event(self.problems_dir, rid, agent, "root_cause", text=root_cause)
        if workaround:
            self._append_event(self.problems_dir, rid, agent, "workaround", text=workaround)

        if note and not new_status:
            self._append_event(self.problems_dir, rid, agent, "note", note=note)

        # Auto-create KEDB entry when a root cause is known.
        if create_kedb:
            prb = self._fold_record(self.problems_dir, rid, Problem)
            if prb.root_cause:
                kedb = self.create_kedb_entry(
                    title=prb.title,
                    symptoms=[],
                    root_cause=prb.root_cause,
                    workaround=prb.workaround or "",
                    related_problem_id=prb.id,
                    managed_by=agent,
                )
                self._append_event(self.problems_dir, rid, agent, "link_kedb", id=kedb.id)

        return self._fold_record(self.problems_dir, rid, Problem)

    def list_problems(self, status: str | None = None) -> list[Problem]:
        """List problems with optional status filter."""
        problems = self._load_records(self.problems_dir, Problem)
        if status:
            problems = [p for p in problems if p.status.value == status]
        return problems

    # ── Changes ───────────────────────────────────────────────────────

    def propose_change(
        self,
        title: str,
        change_type: str = "normal",
        risk: str = "medium",
        rollback_plan: str = "",
        test_plan: str = "",
        managed_by: str = "",
        created_by: str = "",
        implementer: str | None = None,
        related_problem_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Change:
        """Propose a new change (RFC).

        Status is never stored: standard-change auto-approval and CAB outcome
        are pure fold-time derivations.
        """
        agent = managed_by or created_by or "unknown"
        ct = ChangeType(change_type)
        record_id = f"chg-{uuid.uuid4().hex[:8]}"
        core = {
            "id": record_id,
            "type": "change",
            "title": title,
            "label": _slugify(title),
            "change_type": ct.value,
            "risk": Risk(risk).value,
            "rollback_plan": rollback_plan,
            "test_plan": test_plan,
            "managed_by": agent,
            "created_by": created_by or agent,
            "implementer": implementer,
            "cab_required": ct != ChangeType.STANDARD,
            "related_problem_id": related_problem_id,
            "created_at": _now_iso(),
            "tags": tags or [],
        }
        self._write_core(self.changes_dir, record_id, core)
        self._append_event(self.changes_dir, record_id, agent, "created", note=f"RFC: {title}")

        self._publish_event(
            "itil.change.proposed",
            {
                "id": record_id,
                "title": title,
                "change_type": ct.value,
                "cab_required": ct != ChangeType.STANDARD,
            },
        )

        return self._fold_record(self.changes_dir, record_id, Change)

    def update_change(
        self,
        change_id: str,
        agent: str,
        new_status: str | None = None,
        note: str = "",
    ) -> Change:
        """Append a change event and return the folded change.

        Like the other updaters, transition validation is folded (no raise on
        a losing concurrent transition); still raises if the change is unknown.
        """
        rid = self._resolve_id(self.changes_dir, change_id)
        core = self._load_core(self.changes_dir, rid)
        if core is None:
            raise ValueError(f"Change {change_id} not found")

        if new_status:
            self._append_event(self.changes_dir, rid, agent, "status", to=new_status, note=note)
            if new_status == "approved":
                self._publish_event(
                    "itil.change.approved",
                    {
                        "id": rid,
                        "title": core.get("title", ""),
                        "implementer": core.get("implementer"),
                    },
                )
                implementer = core.get("implementer")
                if implementer:
                    gtd_id = self._gtd_emit(
                        f"[ITIL:{rid}] Implement: {core.get('title', '')}",
                        rid,
                        "next",
                        "high",
                    )
                    if gtd_id:
                        self._append_event(self.changes_dir, rid, agent, "gtd_link", id=gtd_id)
            elif new_status == "deployed":
                self._publish_event(
                    "itil.change.deployed",
                    {"id": rid, "title": core.get("title", "")},
                )

        if note and not new_status:
            self._append_event(self.changes_dir, rid, agent, "note", note=note)

        return self._fold_record(self.changes_dir, rid, Change)

    def list_changes(self, status: str | None = None) -> list[Change]:
        """List changes with optional status filter."""
        changes = self._load_records(self.changes_dir, Change)
        if status:
            changes = [c for c in changes if c.status.value == status]
        return changes

    # ── CAB ───────────────────────────────────────────────────────────

    def submit_cab_vote(
        self,
        change_id: str,
        agent: str,
        decision: str = "abstain",
        conditions: str = "",
        subject: str | None = None,
        subject_role: str = "",
        subject_fingerprint: str = "",
        authorization_id: str = "",
    ) -> CABDecision:
        """Submit a CAB vote for a change (per-agent file, already conflict-free).

        The change's approved/rejected status is now a pure fold-time
        derivation from these vote files - no write back to the change record.

        CR change-mgmt P1.4 (CRITICAL): ``agent`` is free text - historically
        nothing bound a vote to an authenticated identity, so any caller could
        write ``agent="human"`` and unblock a change. ``subject`` is the fix's
        skcoord half: pass the caller's authenticated identity here and it -
        never the free-text ``agent`` - becomes the voter of record that
        ``_fold_change`` (and its no-self-approval guard) reads. The upper PEP
        layer (MCP tool / dashboard route, a later card) is responsible for
        resolving the real authenticated subject and always passing it; this
        method does not silently trust a self-declared voter once a subject is
        expected of it - it only ever records what the caller explicitly
        binds. Leaving ``subject`` unset keeps today's free-text behavior for
        direct/legacy callers (CLI, tests), so this is a non-breaking addition.

        Args:
            change_id: The change record this vote applies to.
            agent: Free-text vote label. Used as the voter identity only when
                ``subject`` is not supplied.
            decision: One of ``CABDecisionValue`` (``approved``/``rejected``/
                ``abstain``).
            conditions: Optional free-text conditions attached to the vote.
            subject: Caller-supplied authenticated identity. When set, this
                value - not ``agent`` - is recorded as the voter and used for
                both the vote's filename and its identity in the fold.

        Returns:
            The recorded ``CABDecision`` (``.agent`` is the bound voter
            identity: ``subject`` if given, else ``agent``).
        """
        self.ensure_dirs()
        voter = subject if subject else agent
        if subject_role and subject_role not in {"owner", "operator", "approver", "implementer"}:
            raise ValueError(f"unsupported CAB subject role: {subject_role!r}")
        if subject_role in {"owner", "approver"} and not subject:
            raise ValueError("a qualifying human role requires an authenticated subject")
        vote = CABDecision(
            change_id=change_id,
            agent=voter,
            subject_role=subject_role,
            subject_fingerprint=subject_fingerprint,
            authorization_id=authorization_id,
            decision=CABDecisionValue(decision),
            conditions=conditions,
        )
        filename = f"{change_id}-{voter}.json"
        path = self.cab_dir / filename
        atomic_write_text(path, json.dumps(vote.model_dump(), indent=2, default=str) + "\n")
        return vote

    def get_cab_votes(self, change_id: str) -> list[CABDecision]:
        """Get all CAB votes for a change."""
        votes: list[CABDecision] = []
        if not self.cab_dir.exists():
            return votes
        for f in self.cab_dir.glob(f"{change_id}-*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                votes.append(CABDecision.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return votes

    # ── KEDB ──────────────────────────────────────────────────────────

    def create_kedb_entry(
        self,
        title: str,
        symptoms: list[str],
        root_cause: str = "",
        workaround: str = "",
        permanent_fix_change_id: str | None = None,
        related_problem_id: str | None = None,
        managed_by: str = "",
        tags: list[str] | None = None,
        entry_id: str | None = None,
    ) -> KEDBEntry:
        """Create a Known Error Database entry (write-once, slug dropped).

        ``entry_id`` pins the record id (e.g. a stable ``ke-telegram-wedge`` a
        runbook or adapter references by name); when omitted an ``ke-<uuid>`` id
        is generated as before.
        """
        self.ensure_dirs()
        fields = dict(
            title=title,
            symptoms=symptoms,
            root_cause=root_cause,
            workaround=workaround,
            permanent_fix_change_id=permanent_fix_change_id,
            related_problem_id=related_problem_id,
            managed_by=managed_by,
            tags=tags or [],
        )
        if entry_id is not None:
            fields["id"] = entry_id
        entry = KEDBEntry(**fields)
        path = self.kedb_dir / f"{entry.id}.json"
        atomic_write_text(path, json.dumps(entry.model_dump(), indent=2, default=str) + "\n")
        return entry

    def _load_kedb(self) -> list[KEDBEntry]:
        """Load all KEDB entries (flat write-once files)."""
        entries: list[KEDBEntry] = []
        if not self.kedb_dir.exists():
            return entries
        for f in sorted(self.kedb_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                entries.append(KEDBEntry.model_validate(data))
            except (json.JSONDecodeError, Exception):
                continue
        return entries

    def search_kedb(self, query: str) -> list[KEDBEntry]:
        """Search KEDB entries by matching query against title, symptoms, root_cause."""
        entries = self._load_kedb()
        query_lower = query.lower()
        results = []
        for e in entries:
            searchable = " ".join(
                [
                    e.title,
                    " ".join(e.symptoms),
                    e.root_cause,
                    e.workaround,
                    " ".join(e.tags),
                ]
            ).lower()
            if query_lower in searchable:
                results.append(e)
        return results

    # ── Status dashboard ──────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        """Return a dashboard summary of all ITIL records."""
        incidents = self._load_records(self.incidents_dir, Incident)
        problems = self._load_records(self.problems_dir, Problem)
        changes = self._load_records(self.changes_dir, Change)
        kedb = self._load_kedb()

        open_incidents = [i for i in incidents if i.status.value in OPEN_INCIDENT_STATUSES]
        active_problems = [p for p in problems if p.status.value != "resolved"]
        pending_changes = [
            c
            for c in changes
            if c.status.value in ("proposed", "reviewing", "approved", "scheduled", "implementing")
        ]

        return {
            "incidents": {
                "total": len(incidents),
                "open": len(open_incidents),
                "by_severity": {
                    sev.value: sum(1 for i in open_incidents if i.severity == sev)
                    for sev in Severity
                },
                "open_list": [
                    {
                        "id": i.id,
                        "title": i.title,
                        "severity": i.severity.value,
                        "status": i.status.value,
                        "managed_by": i.managed_by,
                        "detected_at": i.detected_at,
                    }
                    for i in open_incidents
                ],
            },
            "problems": {
                "total": len(problems),
                "active": len(active_problems),
                "active_list": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "status": p.status.value,
                        "managed_by": p.managed_by,
                    }
                    for p in active_problems
                ],
            },
            "changes": {
                "total": len(changes),
                "pending": len(pending_changes),
                "pending_list": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "status": c.status.value,
                        "change_type": c.change_type.value,
                        "managed_by": c.managed_by,
                    }
                    for c in pending_changes
                ],
            },
            "kedb": {
                "total": len(kedb),
            },
        }

    # ── Auto-close / Escalation (for scheduled tasks) ─────────────────

    def auto_close_resolved(self, stable_hours: int = 24) -> list[str]:
        """Auto-close incidents that have been resolved for stable_hours.

        The close is a plain append (via ``update_incident``) to this node's
        own ``auto-close@<host>.jsonl`` writer file; concurrent closes from
        multiple nodes fold idempotently (the first valid close wins).
        """
        now = datetime.now(timezone.utc)
        closed_ids = []
        for inc in self.list_incidents(status="resolved"):
            if inc.resolved_at:
                try:
                    resolved = datetime.fromisoformat(inc.resolved_at.replace("Z", "+00:00"))
                    hours = (now - resolved).total_seconds() / 3600
                    if hours >= stable_hours:
                        self.update_incident(
                            inc.id,
                            "auto-close",
                            new_status="closed",
                            note=f"Auto-closed after {int(hours)}h stable",
                        )
                        closed_ids.append(inc.id)
                except (ValueError, TypeError):
                    continue
        return closed_ids

    def check_sla_breaches(self) -> list[dict[str, Any]]:
        """Check for SLA breaches on open incidents."""
        now = datetime.now(timezone.utc)
        breaches = []
        sla_minutes = {"sev1": 5, "sev2": 15, "sev3": 60, "sev4": 240}

        for inc in self.list_incidents():
            if inc.status.value in ("resolved", "closed"):
                continue
            if inc.status.value == "detected" and inc.detected_at:
                try:
                    detected = datetime.fromisoformat(inc.detected_at.replace("Z", "+00:00"))
                    elapsed_min = (now - detected).total_seconds() / 60
                    limit = sla_minutes.get(inc.severity.value, 60)
                    if elapsed_min > limit:
                        breaches.append(
                            {
                                "id": inc.id,
                                "severity": inc.severity.value,
                                "breach_type": "unacknowledged",
                                "elapsed_minutes": round(elapsed_min),
                                "sla_minutes": limit,
                            }
                        )
                        self._publish_event(
                            "itil.sla.breach",
                            {
                                "id": inc.id,
                                "severity": inc.severity.value,
                                "breach_type": "unacknowledged",
                            },
                        )
                except (ValueError, TypeError):
                    continue
        return breaches

    # ── ITIL Board generation ─────────────────────────────────────────

    def generate_board_md(self) -> str:
        """Generate an ITIL-BOARD.md overview."""
        status = self.get_status()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "# ITIL Service Management Board",
            f"*Auto-generated {now} - do not edit manually*",
            "",
        ]

        # Incidents
        inc = status["incidents"]
        lines.append(f"## Open Incidents ({inc['open']})")
        lines.append("")
        if inc["open_list"]:
            for i in inc["open_list"]:
                sev_icon = {"sev1": "P1", "sev2": "P2", "sev3": "P3", "sev4": "P4"}.get(
                    i["severity"], "?"
                )
                lines.append(
                    f"- **[{i['id']}]** {sev_icon} {i['title']} ({i['status']}) @{i['managed_by']}"
                )
        else:
            lines.append("*No open incidents*")
        lines.append("")

        # Problems
        prb = status["problems"]
        lines.append(f"## Active Problems ({prb['active']})")
        lines.append("")
        if prb["active_list"]:
            for p in prb["active_list"]:
                lines.append(f"- **[{p['id']}]** {p['title']} ({p['status']}) @{p['managed_by']}")
        else:
            lines.append("*No active problems*")
        lines.append("")

        # Changes
        chg = status["changes"]
        lines.append(f"## Pending Changes ({chg['pending']})")
        lines.append("")
        if chg["pending_list"]:
            for c in chg["pending_list"]:
                lines.append(
                    f"- **[{c['id']}]** {c['title']} ({c['status']}, "
                    f"{c['change_type']}) @{c['managed_by']}"
                )
        else:
            lines.append("*No pending changes*")
        lines.append("")

        # KEDB
        lines.append(f"## Known Errors ({status['kedb']['total']})")
        lines.append("")

        return "\n".join(lines)

    def write_board_md(self) -> Path:
        """Write ITIL-BOARD.md to the ITIL directory (run on one pinned node)."""
        self.ensure_dirs()
        content = self.generate_board_md()
        path = self.itil_dir / "ITIL-BOARD.md"
        atomic_write_text(path, content)
        return path

    # ── GTD integration - ITIL is a push adapter on the skos gtd-ingest port ──

    def _gtd_emit(
        self, text: str, source_ref: str, status: str, priority: Optional[str] = None
    ) -> Optional[str]:
        """Emit a GTD item through the skos gtd-ingest capture() sink (deduped by
        (source, source_ref)). Falls back to the legacy direct writer if skos is
        not importable, so skcapstone never hard-depends on skos at runtime.
        See skos/docs/gtd-ingest-architecture.md."""
        try:
            from skos.gtd_ingest import GtdCapture, capture  # the port

            return capture(
                GtdCapture(
                    text=text,
                    source="itil",
                    source_ref=source_ref,
                    context="@ops",
                    priority=priority,
                    status=status,
                    meta={"itil_id": source_ref},
                )
            )
        except Exception:
            try:
                from skcapstone.mcp_tools.gtd_tools import _load_list, _make_item, _save_list

                list_for = {
                    "next": "next-actions",
                    "project": "projects",
                    "waiting": "waiting-for",
                    "someday": "someday-maybe",
                }.get(status, "inbox")
                item = _make_item(text=text, source="itil", context="@ops")
                item["status"] = status
                item["source_ref"] = source_ref
                if priority:
                    item["priority"] = priority
                items = _load_list(list_for)
                items.append(item)
                _save_list(list_for, items)
                return item["id"]
            except Exception:
                logger.debug("Failed to emit GTD item for %s", source_ref)
                return None

    def _create_gtd_item_for_incident(self, incident: Incident) -> Optional[str]:
        """Auto-create a GTD next-action (sev1/sev2) or inbox item (sev3/sev4)."""
        priority = {"sev1": "critical", "sev2": "high", "sev3": "medium", "sev4": "low"}.get(
            incident.severity.value, "medium"
        )
        status = "next" if incident.severity.value in ("sev1", "sev2") else "inbox"
        return self._gtd_emit(
            f"[ITIL:{incident.id}] {incident.title}", incident.id, status, priority
        )

    def _create_gtd_project_for_problem(self, problem: Problem) -> Optional[str]:
        """Auto-create a GTD project for a problem investigation."""
        return self._gtd_emit(
            f"[ITIL:{problem.id}] Investigate: {problem.title}", problem.id, "project"
        )

    def _complete_gtd_items(self, gtd_item_ids: list[str]) -> None:
        """Mark linked GTD items as done when the owning ITIL record resolves."""
        try:
            from skcapstone.mcp_tools.gtd_tools import (
                _find_item_across_lists,
                _load_archive,
                _remove_item_from_list,
                _save_archive,
            )

            for item_id in gtd_item_ids:
                source_list, item, _ = _find_item_across_lists(item_id)
                if source_list and item:
                    _remove_item_from_list(source_list, item_id)
                    item["status"] = "done"
                    item["completed_at"] = _now_iso()
                    archive = _load_archive()
                    archive.append(item)
                    _save_archive(archive)
        except Exception:
            logger.debug("Failed to complete GTD items: %s", gtd_item_ids)

    def _itil_ref_is_orphan(self, itil_id: str) -> bool:
        """True when an ITIL-linked GTD ref has no *open* owning record.

        Chef's rule for the recurring orphan storm: an open incident must exist
        before an ITIL GTD item is valid. A ref is an orphan when its core never
        persisted (or diverged across synced nodes so it exists nowhere here), or
        - for incidents specifically - the record exists but is no longer open
        (a closed/resolved incident should have had its GTD item completed, not
        left dangling as an active next-action).

        Problems and changes are long-lived by design, so they are only treated
        as orphans when the record is entirely missing, never on status.

        Args:
            itil_id: The ``inc-``/``prb-``/``chg-`` id carried by the GTD item.

        Returns:
            True if the linked item should be reaped.
        """
        prefix = str(itil_id).split("-", 1)[0]
        directory = {
            "inc": self.incidents_dir,
            "prb": self.problems_dir,
            "chg": self.changes_dir,
        }.get(prefix)
        if directory is None:
            return False  # unknown/foreign ref - leave it alone
        rid = self._resolve_id(directory, itil_id)
        if not self._record_exists(directory, rid):
            return True  # no core anywhere -> orphan
        if prefix == "inc":
            inc = self._fold_record(directory, rid, Incident)
            if inc is None or inc.status.value not in OPEN_INCIDENT_STATUSES:
                return True  # exists but not open -> stale, reap
        return False

    def reconcile_gtd_orphans(self) -> list[str]:
        """Drain ITIL-linked GTD items whose owning record is not an open incident.

        Idempotent reconcile sweep - the read-side complement to the create-side
        guard in :meth:`create_incident`. It defends against orphans that arrive
        by any path the create guard cannot cover: GTD next-actions synced in from
        a node whose incident core never converged, refs left behind by a since-
        patched writer, or items for incidents that have since closed. Each reaped
        item is moved to the GTD archive (status ``dropped``) rather than deleted,
        so the action is auditable and reversible.

        Returns:
            The list of reaped GTD item ids (empty when nothing was orphaned).
        """
        reaped: list[str] = []
        try:
            from skcapstone.mcp_tools.gtd_tools import (
                _GTD_LISTS,
                _load_archive,
                _load_list,
                _save_archive,
                _save_list,
            )
        except Exception:
            logger.debug("gtd_tools not importable - skipping ITIL GTD reconcile")
            return reaped

        for list_name in _GTD_LISTS:
            try:
                items = _load_list(list_name)
            except Exception:
                continue
            keep: list[dict] = []
            dropped: list[dict] = []
            for item in items:
                itil_id = item.get("itil_id") or item.get("source_ref")
                if not itil_id:
                    # Legacy items (pre-structured-fields) carry the id only in the
                    # ``[ITIL:inc-XXXX]`` text prefix - parse it as a fallback so the
                    # sweep reaps them too.
                    m = re.search(r"\[ITIL:((?:inc|prb|chg)-[0-9a-f]+)\]", item.get("text", ""))
                    if m:
                        itil_id = m.group(1)
                if (
                    itil_id
                    and str(itil_id).startswith(("inc-", "prb-", "chg-"))
                    and self._itil_ref_is_orphan(str(itil_id))
                ):
                    item["status"] = "dropped"
                    item["completed_at"] = _now_iso()
                    item["drop_reason"] = "itil-orphan-reconcile: no open incident record"
                    dropped.append(item)
                    reaped.append(item.get("id"))
                else:
                    keep.append(item)
            if dropped:
                try:
                    archive = _load_archive()
                    archive.extend(dropped)
                    _save_archive(archive)
                    _save_list(list_name, keep)
                except Exception:
                    logger.warning("Failed to persist ITIL GTD reconcile for %s", list_name)

        if reaped:
            logger.info(
                "ITIL GTD reconcile: reaped %d orphan next-action(s): %s", len(reaped), reaped
            )
        else:
            logger.debug("ITIL GTD reconcile: no orphaned next-actions")
        return reaped

    # ── PubSub helper ─────────────────────────────────────────────────

    def _publish_event(self, topic: str, payload: dict) -> None:
        """Publish an ITIL event via PubSub (best-effort)."""
        try:
            from skcapstone.pubsub import PubSub

            agent_name = payload.get("managed_by", "itil-system")
            bus = PubSub(self.home, agent_name=agent_name)
            bus.publish(topic, payload, ttl_seconds=86400)
        except Exception:
            logger.debug("Failed to publish event %s", topic)

        # Also push to activity bus
        try:
            from skcapstone import activity

            activity.push(topic, payload)
        except Exception as exc:
            logger.warning("Failed to push ITIL event %s to activity bus: %s", topic, exc)
