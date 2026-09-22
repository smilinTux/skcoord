"""Unified kanban Card projection over coord tasks and ITIL tickets.

Phase 1 is read-only: a ``Card`` is a projection, never a stored record. The
sources of truth remain ``coordination/`` (tasks + agent files) and ``itil/``
(event-sourced records). Columns are the shared lifecycle; swimlanes are the
card ``kind``. See docs/superpowers/specs/2026-07-16-unified-kanban-card-model.md.
"""

from __future__ import annotations

import fcntl
import html
import logging
import os
import re
import socket
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field

from .coordination import Board, TaskStatus, TaskView, validate_shared_home

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC now as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class Kind(str, Enum):
    """The type of work a card represents (drives its swimlane)."""

    TASK = "task"
    EPIC = "epic"
    INCIDENT = "incident"
    PROBLEM = "problem"
    CHANGE = "change"


class Column(str, Enum):
    """The kanban lifecycle stage (shared by every kind)."""

    BACKLOG = "backlog"
    READY = "ready"
    DOING = "doing"
    REVIEW = "review"
    DONE = "done"


class Card(BaseModel):
    """A single work item projected onto the kanban board."""

    id: str
    kind: Kind
    title: str
    description: str = ""
    status: Column
    swimlane: str
    priority: str = "medium"
    originator: str = ""
    owner: str | None = None
    order: int = 0
    labels: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    links: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)
    archived: bool = False
    created_at: str = ""
    updated_at: str = ""
    source: str = "coord"


# ---------------------------------------------------------------------------
# Kanban overlay events (Phase 3): explicit moves, order, labels, links
# ---------------------------------------------------------------------------


class CardEvent(BaseModel):
    """One kanban overlay event (move, order, label, link, priority, swimlane,
    describe).

    Overlay events let a human or agent operate the board (move a card to a
    column, order it, tag it) without touching coord's claim-based write path.
    """

    model_config = ConfigDict(extra="forbid")

    card_id: str
    action: str
    # Optional cross-store identity. Records written before graph-truth
    # verification do not have this field and remain valid.
    event_id: str | None = None
    writer: str = ""
    ts: str = Field(default_factory=_now_iso)
    seq: int = 0
    column: str | None = None
    order: int | None = None
    priority: str | None = None
    swimlane: str | None = None
    label: str | None = None
    link_key: str | None = None
    link_value: str | None = None
    owner: str | None = None
    title: str | None = None
    description: str | None = None


# The only actions the fold can apply. Kept here, next to the writer, so the
# append point can reject anything the fold would silently drop; it is asserted
# equal to card_store._OVERLAY_TO_STORE_ACTION by the test suite so the two
# cannot drift apart. Measured 2026-09-19: an event with action "verdict" was
# written to chiap02.jsonl, was perfectly well-formed, and folded to nothing at
# all, because "verdict" was not in this set. "verdict" is now mapped (to
# "link" in card_store._OVERLAY_TO_STORE_ACTION), so it belongs here too.
OVERLAY_ACTIONS = frozenset(
    {
        "move",
        "set_priority",
        "set_swimlane",
        "add_label",
        "remove_label",
        "link",
        "assign",
        "unassign",
        "describe",
        "verdict",
    }
)

_OVERLAY_PAYLOAD_FIELDS = frozenset(
    {
        "column",
        "order",
        "priority",
        "swimlane",
        "label",
        "link_key",
        "link_value",
        "owner",
        "title",
        "description",
    }
)
_ACTION_FIELDS = {
    "move": frozenset({"column", "order"}),
    "set_priority": frozenset({"priority"}),
    "set_swimlane": frozenset({"swimlane"}),
    "add_label": frozenset({"label"}),
    "remove_label": frozenset({"label"}),
    "link": frozenset({"link_key", "link_value"}),
    "verdict": frozenset({"link_key", "link_value"}),
    "assign": frozenset({"owner"}),
    "unassign": frozenset(),
    "describe": frozenset({"title", "description"}),
}
_WRITER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}\Z")


def validate_overlay_event(event: CardEvent, *, require_writer: bool = True) -> None:
    """Validate one overlay event at the shared read and write boundary."""
    if event.action not in OVERLAY_ACTIONS:
        raise ValueError(
            f"unsupported overlay action {event.action!r}; expected one of "
            f"{sorted(OVERLAY_ACTIONS)}"
        )
    if require_writer and not _WRITER_RE.fullmatch(event.writer):
        raise ValueError("overlay writer must be a nonempty bounded identity")

    allowed = _ACTION_FIELDS[event.action]
    populated = {name for name in _OVERLAY_PAYLOAD_FIELDS if getattr(event, name) is not None}
    reserved = populated - allowed
    if reserved:
        raise ValueError(
            f"overlay action {event.action!r} carries reserved field(s): "
            f"{', '.join(sorted(reserved))}"
        )

    if event.action == "move" and not populated:
        raise ValueError("overlay action 'move' requires column or order")
    if event.action in {"set_priority", "set_swimlane", "add_label", "remove_label"}:
        field = next(iter(allowed))
        if not str(getattr(event, field) or "").strip():
            raise ValueError(f"overlay action {event.action!r} requires {field}")
    if event.action in {"link", "verdict"} and (
        not str(event.link_key or "").strip() or event.link_value is None
    ):
        raise ValueError(f"overlay action {event.action!r} requires link_key and link_value")
    if event.action == "verdict" and event.link_key != "verdict":
        raise ValueError("overlay action 'verdict' requires link_key='verdict'")
    if event.action == "assign" and not str(event.owner or "").strip():
        raise ValueError("overlay action 'assign' requires owner")
    if event.action == "describe" and event.title is None and event.description is None:
        raise ValueError("overlay action 'describe' requires title or description")


# How much of a rejected line to quote in the log. Long enough to identify the
# line by eye, short enough that one huge bad line cannot flood a log file.
_EXCERPT_CHARS = 200

# Per-file cap on individual warnings, so a shard with thousands of bad lines
# reports a handful plus a total rather than thousands of log records.
_MAX_WARNINGS_PER_FILE = 5

#: Lines already warned about in THIS process, as (file name, line number).
#:
#: `_MAX_WARNINGS_PER_FILE` caps warnings per fold() CALL, and fold() is called
#: once per card. A selector cycle folds thousands of cards, so ONE malformed
#: overlay line produces the same warning thousands of times in a single run.
#:
#: Measured 2026-09-19 on the chi estate: a single bad line in chiap08.jsonl
#: flooded every fleet worker log and every CLI invocation, and pushed the seat
#: dispatcher's JSON receipt past journald's 48KB message cap, so the receipt
#: arrived truncated mid-string and was unparseable. Operators could not see why
#: dispatch failed, and the seat looked silent while burning two minutes of CPU
#: per cycle. One unreadable line cost the observability of the whole fleet.
#:
#: Warn once per distinct line. Every occurrence is still recorded in
#: `rejected`, which is where callers that want the full picture already look,
#: so this suppresses repetition and not information.
_WARNED_LINES: set[tuple[str, int]] = set()


class CardEventLog:
    """Per-writer append-only overlay log for kanban operations.

    Conflict-free: every writer appends only to
    ``coordination/card_events/<host>.jsonl`` (same invariant as the agent
    files and the archive index).
    """

    def __init__(self, home: Path) -> None:
        # Lines the most recent read_all() refused, as dicts carrying file,
        # line, excerpt and error. Exposed so a health check can assert on the
        # damage rather than having to scrape log output for it.
        self.rejected: list[dict] = []
        self.home = validate_shared_home(home)
        self.dir = self.home / "coordination" / "card_events"

    @staticmethod
    def _open_or_create_directory(parent_fd: int, name: str) -> int:
        """Open a direct child directory without following a raced symlink."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("safe card event paths require O_NOFOLLOW support")
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        else:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                raise ValueError("card event directory must not be a symlink")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | no_follow,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise ValueError("card event directory is unsafe") from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("card event directory is unsafe")
        return descriptor

    def _open_event_directory(self) -> int:
        """Pin coordination/card_events while append holds its descriptor."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("safe card event paths require O_NOFOLLOW support")
        self.home.mkdir(parents=True, exist_ok=True)
        if self.home.is_symlink():
            raise ValueError("card event home must not be a symlink")
        try:
            home_fd = os.open(
                self.home,
                os.O_RDONLY | os.O_DIRECTORY | no_follow,
            )
        except OSError as exc:
            raise ValueError("card event home is unsafe") from exc
        try:
            coordination_fd = self._open_or_create_directory(home_fd, "coordination")
            try:
                return self._open_or_create_directory(coordination_fd, "card_events")
            finally:
                os.close(coordination_fd)
        finally:
            os.close(home_fd)

    @staticmethod
    def _open_existing_directory(parent_fd: int, name: str) -> int | None:
        """Open an existing direct child directory without following links."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("safe card event paths require O_NOFOLLOW support")
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
            raise ValueError("card event directory is unsafe")
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | no_follow,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise ValueError("card event directory is unsafe") from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("card event directory is unsafe")
        return descriptor

    def _open_existing_event_directory(self) -> int | None:
        """Open existing coordination/card_events without creating it."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("safe card event paths require O_NOFOLLOW support")
        if self.home.is_symlink():
            raise ValueError("card event home is unsafe")
        try:
            home_fd = os.open(
                self.home,
                os.O_RDONLY | os.O_DIRECTORY | no_follow,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("card event home is unsafe") from exc
        try:
            coordination_fd = self._open_existing_directory(home_fd, "coordination")
            if coordination_fd is None:
                return None
            try:
                return self._open_existing_directory(coordination_fd, "card_events")
            finally:
                os.close(coordination_fd)
        finally:
            os.close(home_fd)

    @contextmanager
    def writer_lock(self, filename: str) -> Iterator[int]:
        """Hold the shared overlay writer lock and yield the pinned directory."""
        if (
            not filename.endswith(".jsonl")
            or not filename
            or "/" in filename
            or "\\" in filename
            or ".." in filename
        ):
            raise ValueError("card event writer filename is unsafe")
        directory_fd = self._open_event_directory()
        lock_name = f".{filename}.lock"
        descriptor = -1
        try:
            try:
                existing = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode)
                or not stat.S_ISREG(existing.st_mode)
                or existing.st_nlink != 1
            ):
                raise ValueError("card event writer lock is unsafe")
            descriptor = os.open(
                lock_name,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ValueError("card event writer lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield directory_fd
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(directory_fd)

    @staticmethod
    def _read_regular_file_bytes(directory_fd: int, name: str) -> bytes | None:
        """Read one regular single-link event file through its parent fd."""
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("safe card event paths require O_NOFOLLOW support")
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError("card event source is unsafe")
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
        ):
            raise ValueError("card event source is unsafe")
        try:
            descriptor = os.open(name, os.O_RDONLY | no_follow, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError("card event source is unsafe") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (existing.st_dev, existing.st_ino)
            ):
                raise ValueError("card event source is unsafe")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 65536):
                chunks.append(chunk)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ):
                raise ValueError("card event source changed while reading")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def append(self, event: CardEvent) -> None:
        """Append one overlay event to this host's log.

        Two shape guards run before anything else, both at this one shared
        append point rather than at the eight-odd call sites:

        * ``action`` must be one the fold can actually apply. It was a
          free-form ``str``, so a caller could write a flawless ``verdict``
          event that ``fold`` then discarded as an unknown action, with no
          error at either end.
        * the serialized event must be exactly one line. One event is one line
          in a JSONL ledger; a newline anywhere in the payload would split the
          record into an unparseable head and an unparseable tail.
        """
        from .card_store import CardStore

        if not _WRITER_RE.fullmatch(event.writer):
            raise ValueError("overlay writer must be a nonempty bounded identity")
        if event.action not in OVERLAY_ACTIONS:
            validate_overlay_event(event, require_writer=False)

        store = None
        if (
            event.action in {"describe", "link"}
            or (self.home / "cards" / event.card_id / "core.json").exists()
        ):
            store = CardStore(self.home)
        if event.action in {"describe", "link"} and (
            store is None or store.fold(event.card_id) is None
        ):
            raise ValueError(f"CardStore card {event.card_id} has no foldable core")
        if (
            store is not None
            and event.action in {"move", "assign", "unassign"}
            and any(item.get("action") == "void" for item in store._read_events(event.card_id))
        ):
            raise ValueError(
                f"CardStore card {event.card_id} is voided; void is a terminal decision"
            )
        filename = f"{socket.gethostname()}.jsonl"
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        with self.writer_lock(filename) as directory_fd:
            validate_overlay_event(event)
            try:
                existing = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (
                stat.S_ISLNK(existing.st_mode)
                or not stat.S_ISREG(existing.st_mode)
                or existing.st_nlink != 1
            ):
                raise ValueError("card event destination is unsafe")
            descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
            event_stat = os.fstat(descriptor)
            if not stat.S_ISREG(event_stat.st_mode) or event_stat.st_nlink != 1:
                os.close(descriptor)
                descriptor = -1
                raise ValueError("card event destination is unsafe")
            with os.fdopen(descriptor, "a+", encoding="utf-8") as fh:
                descriptor = -1
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.seek(0)
                    for number, line in enumerate(fh, start=1):
                        if not line.strip():
                            continue
                        try:
                            existing_event = CardEvent.model_validate_json(line)
                            validate_overlay_event(existing_event)
                        except Exception as exc:  # noqa: BLE001
                            raise ValueError(
                                f"existing overlay shard {filename} line {number} "
                                f"is invalid: {type(exc).__name__}: {exc}"
                            ) from exc
                    payload = event.model_dump_json()
                    if "\n" in payload or "\r" in payload:
                        raise ValueError(
                            "overlay event must serialize to a single line; "
                            "a newline in the payload would split the ledger record"
                        )
                    fh.seek(0, os.SEEK_END)
                    fh.write(payload + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            os.fsync(directory_fd)
        if descriptor >= 0:
            os.close(descriptor)

    def read_all(self) -> list[CardEvent]:
        """Read every overlay event across all writers.

        A line this cannot admit is REPORTED, never dropped in silence. It is
        recorded in ``self.rejected`` and logged at WARNING with the shard, the
        physical line number and an excerpt, because this ledger is one of the
        two stores card outcomes are read from and a skipped line is a lost
        outcome.

        This deliberately keeps going rather than raising. The overlay is
        fleet-wide: every host appends to its own ``<host>.jsonl`` and Syncthing
        replicates all of them into every other host, so one bad line written
        anywhere would, if this raised, stop dispatch everywhere at once. The
        per-card structure store (``CardStore._read_events``) is the one that
        fails closed, and correctly so: there the blast radius of refusing is a
        single card. Answering from the surviving record and shouting about the
        gap beats refusing to answer at all; what is not acceptable, and what
        this fixes, is answering from a damaged record and saying nothing.
        """
        out: list[CardEvent] = []
        self.rejected = []
        directory_fd = self._open_existing_event_directory()
        if directory_fd is None:
            return out
        try:
            for name in sorted(os.listdir(directory_fd)):
                if not name.endswith(".jsonl"):
                    continue
                raw = self._read_regular_file_bytes(directory_fd, name)
                if raw is None:
                    continue
                bad_in_file = 0
                for number, line in enumerate(raw.splitlines(), start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = CardEvent.model_validate_json(line)
                        validate_overlay_event(event)
                        out.append(event)
                    except Exception as exc:  # noqa: BLE001
                        bad_in_file += 1
                        excerpt = line[:_EXCERPT_CHARS].decode("utf-8", errors="replace")
                        self.rejected.append(
                            {
                                "file": name,
                                "line": number,
                                "excerpt": excerpt,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        already_warned = (name, number) in _WARNED_LINES
                        if bad_in_file <= _MAX_WARNINGS_PER_FILE and not already_warned:
                            _WARNED_LINES.add((name, number))
                            logger.warning(
                                "card_events %s line %d is not a card event, dropping it "
                                "from the fold: %s | %r",
                                name,
                                number,
                                type(exc).__name__,
                                excerpt,
                            )
                if bad_in_file > _MAX_WARNINGS_PER_FILE:
                    logger.warning(
                        "card_events %s: %d unreadable lines total (%d more beyond the "
                        "ones logged above)",
                        name,
                        bad_in_file,
                        bad_in_file - _MAX_WARNINGS_PER_FILE,
                    )
        finally:
            os.close(directory_fd)
        return out


def fold_overlay(events: list[CardEvent]) -> dict[str, dict]:
    """Fold overlay events into a per-card patch dict.

    Events apply in ``(ts, writer, seq)`` order: ``move`` sets column + order
    (last wins), ``set_priority``/``set_swimlane`` last wins, ``add_label``/
    ``remove_label`` accumulate, ``link`` merges into ``links``, ``assign``/
    ``unassign`` set/clear owner (``owner_set`` marks an explicit change so
    None-from-unassign is distinguishable from never-touched), ``describe``
    sets title/description last-wins (only the keys the event actually carries,
    so None stays "never touched" and "" is a deliberate clear).
    """
    ordered = sorted(events, key=lambda e: (e.ts, e.writer, e.seq))
    overlay: dict[str, dict] = {}
    for e in ordered:
        patch = overlay.setdefault(
            e.card_id,
            {
                "column": None,
                "order": None,
                "priority": None,
                "swimlane": None,
                "labels": [],
                "links": {},
                "owner": None,
                "owner_set": False,
                "title": None,
                "description": None,
            },
        )
        if e.action == "move":
            if e.column is not None:
                patch["column"] = e.column
            if e.order is not None:
                patch["order"] = e.order
        elif e.action == "set_priority" and e.priority is not None:
            patch["priority"] = e.priority
        elif e.action == "set_swimlane" and e.swimlane is not None:
            patch["swimlane"] = e.swimlane
        elif e.action == "add_label" and e.label and e.label not in patch["labels"]:
            patch["labels"].append(e.label)
        elif e.action == "remove_label" and e.label in patch["labels"]:
            patch["labels"].remove(e.label)
        elif e.action == "link" and e.link_key is not None:
            patch["links"][e.link_key] = e.link_value
        elif e.action == "assign" and e.owner:
            patch["owner"] = e.owner
            patch["owner_set"] = True
        elif e.action == "unassign":
            patch["owner"] = None
            patch["owner_set"] = True
        elif e.action == "describe":
            if e.title is not None:
                patch["title"] = e.title
            if e.description is not None:
                patch["description"] = e.description
    return overlay


# ---------------------------------------------------------------------------
# coord TaskView -> Card
# ---------------------------------------------------------------------------

_STATUS_TO_COLUMN = {
    TaskStatus.OPEN: Column.BACKLOG,
    TaskStatus.CLAIMED: Column.READY,
    TaskStatus.IN_PROGRESS: Column.DOING,
    TaskStatus.REVIEW: Column.REVIEW,
    TaskStatus.DONE: Column.DONE,
    TaskStatus.BLOCKED: Column.DOING,
}


def _swimlane_for_tags(tags: list[str]) -> str:
    """Pick a swimlane for a coord task from its tags."""
    lowered = {t.lower() for t in tags}
    if "autopilot-staged" in lowered:
        return "proposed"
    if "bug" in lowered:
        return "bug"
    if "security" in lowered:
        return "security"
    return "feature"


def card_from_taskview(view: TaskView) -> Card:
    """Project a coord ``TaskView`` into a kanban ``Card``."""
    t = view.task
    tags_lower = {x.lower() for x in t.tags}
    kind = Kind.EPIC if "epic" in tags_lower else Kind.TASK
    meta = dict(t.meta)
    if view.status == TaskStatus.BLOCKED:
        meta["blocked"] = True
    return Card(
        id=t.id,
        kind=kind,
        title=t.title,
        description=t.description,
        status=_STATUS_TO_COLUMN[view.status],
        swimlane=_swimlane_for_tags(t.tags),
        priority=t.priority.value,
        originator=t.created_by,
        owner=view.claimed_by,
        labels=list(t.tags),
        acceptance_criteria=list(t.acceptance_criteria),
        dependencies=list(t.dependencies),
        meta=meta,
        created_at=t.created_at,
        source="coord",
    )


# ---------------------------------------------------------------------------
# ITIL records -> Card
# ---------------------------------------------------------------------------

# Column maps keyed by the REAL itil.py enum ``.value`` strings.
_INCIDENT_COLUMN = {
    "detected": Column.DOING,
    "acknowledged": Column.DOING,
    "investigating": Column.DOING,
    "escalated": Column.DOING,
    "resolved": Column.REVIEW,
    "closed": Column.DONE,
}
_PROBLEM_COLUMN = {
    "identified": Column.READY,
    "analyzing": Column.DOING,
    "known_error": Column.REVIEW,
    "resolved": Column.DONE,
}
# Change-mgmt P2.4 (docs/specs/2026-08-13-change-management-cab-ai-arch.md
# section 8): "scheduled" joins the ready column alongside reviewing/approved
# (a scheduled change is still awaiting its deploy window, not yet doing).
# The full lane: backlog=proposed; ready=reviewing/approved/scheduled;
# doing=implementing/failed; review=deployed; done=verified/closed/rejected.
_CHANGE_COLUMN = {
    "proposed": Column.BACKLOG,
    "reviewing": Column.READY,
    "approved": Column.READY,
    "scheduled": Column.READY,
    "implementing": Column.DOING,
    "failed": Column.DOING,
    "deployed": Column.REVIEW,
    "verified": Column.DONE,
    "closed": Column.DONE,
    "rejected": Column.DONE,
}


def card_from_incident(inc) -> Card:
    """Project an ITIL ``Incident`` into a kanban ``Card`` (expedite lane)."""
    return Card(
        id=inc.id,
        kind=Kind.INCIDENT,
        title=inc.title,
        status=_INCIDENT_COLUMN.get(inc.status.value, Column.DOING),
        swimlane="expedite",
        priority="high",
        meta={"severity": inc.severity.value, "itil_status": inc.status.value},
        source="itil",
    )


def card_from_problem(p) -> Card:
    """Project an ITIL ``Problem`` into a kanban ``Card`` (problem lane)."""
    return Card(
        id=p.id,
        kind=Kind.PROBLEM,
        title=p.title,
        status=_PROBLEM_COLUMN.get(p.status.value, Column.DOING),
        swimlane="problem",
        meta={"itil_status": p.status.value},
        source="itil",
    )


def _change_window_missed(events: list[dict]) -> bool:
    """True if this change's most recent schedule-lifecycle event was a miss.

    ``scheduled_window`` is cleared by BOTH an explicit ``unschedule`` and a
    missed window (see ``_fold_change`` in skcoord.itil), so its mere absence
    on the folded ``Change`` cannot distinguish "operator unscheduled it" from
    "the window came and went". The folded timeline collapses both to the
    same generic ``status:scheduled->approved`` action string too. This walks
    the RAW event log instead (``ITILManager._read_events``, already sorted
    the same way the fold replays it), which still carries each event's
    original ``kind`` - precise, no note-text guessing required.

    Args:
        events: This change's raw events, e.g.
            ``ITILManager._read_events(mgr.changes_dir, change_id)``.

    Returns:
        bool: Whether the last ``schedule``/``unschedule``/``window_missed``
        event in the log was specifically a ``window_missed``. Only
        meaningful when the folded change currently has no
        ``scheduled_window``; callers check that first.
    """
    lifecycle = {"schedule", "unschedule", "window_missed"}
    last_kind = None
    for e in events:
        if e.get("kind") in lifecycle:
            last_kind = e.get("kind")
    return last_kind == "window_missed"


def card_from_change(ch, events: list[dict] | None = None) -> Card:
    """Project an ITIL ``Change`` into a kanban ``Card`` (change lane).

    Change-mgmt P2.4: passes the P1.1 fold fields (``prepared_pr``,
    ``prepared_by``, ``validation``, ``scheduled_window``) through into
    ``meta`` so a dashboard client (chips, popout) renders the change's full
    state without a second fetch of the raw ITIL record.

    Args:
        ch: The folded ``Change``.
        events: This change's raw event log (see ``_change_window_missed``),
            or ``None`` when the caller has not fetched it - the
            ``window_missed`` meta flag then conservatively defaults to
            ``False`` rather than guessing.
    """
    window_missed = _change_window_missed(events) if events is not None else False
    return Card(
        id=ch.id,
        kind=Kind.CHANGE,
        title=ch.title,
        status=_CHANGE_COLUMN.get(ch.status.value, Column.BACKLOG),
        swimlane="change",
        meta={
            "itil_status": ch.status.value,
            "prepared_pr": ch.prepared_pr,
            "prepared_by": ch.prepared_by,
            "validation": ch.validation,
            "scheduled_window": ch.scheduled_window,
            "window_missed": window_missed,
        },
        source="itil",
    )


# ---------------------------------------------------------------------------
# KanbanBoard projection
# ---------------------------------------------------------------------------

COLUMN_ORDER = [c.value for c in Column]  # backlog, ready, doing, review, done
LANE_ORDER = ["feature", "bug", "security", "expedite", "change", "problem", "proposed"]
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# WIP limits per column (backlog/done are unlimited). The expedite/incident
# lane bypasses these by design.
WIP_LIMITS = {"ready": 8, "doing": 6, "review": 4}


class KanbanBoard:
    """Read-only kanban projection over the coord board and the ITIL store.

    Args:
        home: Path to the shared skcapstone root (``~/.skcapstone``).
    """

    def __init__(self, home: Path) -> None:
        self.home = validate_shared_home(home)

    def cards(self, include_archived: bool = False) -> list[Card]:
        """All cards from both sources, with the kanban overlay applied.

        When the ``SKCOORD_CARD_STORE=1`` flag is set, the board is served from
        the event-sourced CardStore (Phase 4) instead of the legacy projection.
        Default (flag unset/0) keeps the legacy coord + ITIL + overlay path.

        Args:
            include_archived: When True, archived coord tasks are included with
                their ``archived`` flag set (used by the Phase 4 importer and
                parity check). Default False keeps the active-board behavior.
        """
        from .card_store import card_store_read_enabled

        if card_store_read_enabled():
            from .card_store import CardStore

            store_cards = CardStore(self.home).list_cards(
                include_archived=include_archived, degrade_unreadable=True
            )
            # Catastrophe guard: never silently empty the board if the store is
            # behind. Fall back to the legacy projection when the store is empty
            # but legacy task files exist.
            if store_cards or not any(Board(self.home).tasks_dir.glob("*.json")):
                return store_cards
            logger.warning(
                "CardStore empty but legacy task files exist; serving legacy "
                "projection (catastrophe guard)."
            )

        out: list[Card] = []
        board = Board(self.home)
        archived_ids = board.archived_ids()
        for view in board.get_task_views(include_archived=include_archived):
            c = card_from_taskview(view)
            if c.id in archived_ids:
                c.archived = True
            out.append(c)
        try:
            from .itil import ITILManager

            mgr = ITILManager(self.home)
            out += [card_from_incident(i) for i in mgr.list_incidents()]
            out += [card_from_problem(p) for p in mgr.list_problems()]
            for c in mgr.list_changes():
                events = mgr._read_events(mgr.changes_dir, c.id)
                out.append(card_from_change(c, events))
        except Exception:  # ITIL store may be absent; projection stays task-only
            pass

        # Apply the kanban overlay (explicit moves, order, labels, links).
        overlay = fold_overlay(CardEventLog(self.home).read_all())
        valid_cols = {c.value for c in Column}
        from .card_store import CardStore

        store = CardStore(self.home)
        voided_ids = {
            c.id
            for c in out
            if any(event.get("action") == "void" for event in store._read_events(c.id))
        }
        for c in out:
            if c.id in voided_ids:
                c.archived = True
                c.owner = None
                continue
            patch = overlay.get(c.id)
            if not patch:
                continue
            if patch["column"] in valid_cols:
                c.status = Column(patch["column"])
            if patch["order"] is not None:
                c.order = patch["order"]
            if patch["priority"]:
                c.priority = patch["priority"]
            if patch["swimlane"]:
                c.swimlane = patch["swimlane"]
            for lb in patch["labels"]:
                if lb not in c.labels:
                    c.labels.append(lb)
            c.links.update(patch["links"])
            if patch.get("owner_set"):
                c.owner = patch["owner"]
            if patch.get("title") is not None:
                c.title = patch["title"]
            if patch.get("description") is not None:
                c.description = patch["description"]

        if include_archived:
            return out
        return [c for c in out if not c.archived]

    def grid(self) -> dict[str, dict[str, list[Card]]]:
        """Group active cards as ``grid[swimlane][column] -> [cards]``.

        Cards within a cell are ordered by explicit order (when set), then
        priority, then id.
        """
        grid: dict[str, dict[str, list[Card]]] = {
            lane: {col: [] for col in COLUMN_ORDER} for lane in LANE_ORDER
        }
        for c in self.cards():
            lane = c.swimlane if c.swimlane in grid else "feature"
            grid[lane][c.status.value].append(c)
        for lane in grid.values():
            for col in lane.values():
                col.sort(
                    key=lambda c: (
                        c.order if c.order else 9999,
                        _PRIORITY_RANK.get(c.priority, 2),
                        c.id,
                    )
                )
        return grid

    def wip_report(self) -> dict[str, dict]:
        """Per-column WIP status. The expedite lane is excluded (bypasses WIP).

        Returns:
            dict: ``report[column] = {"count", "limit", "over"}``.
        """
        counts = {col: 0 for col in COLUMN_ORDER}
        for c in self.cards():
            if c.swimlane == "expedite":
                continue
            counts[c.status.value] += 1
        report: dict[str, dict] = {}
        for col in COLUMN_ORDER:
            limit = WIP_LIMITS.get(col)
            report[col] = {
                "count": counts[col],
                "limit": limit,
                "over": limit is not None and counts[col] > limit,
            }
        return report


# ---------------------------------------------------------------------------
# HTML render (self-contained, both themes, escaped, no em/en dashes)
# ---------------------------------------------------------------------------

_LANE_META = {
    "feature": ("Feature", "kind: task / epic"),
    "bug": ("Bug", "kind: task"),
    "security": ("Security", "kind: task"),
    "expedite": ("Expedite", "kind: incident"),
    "change": ("Change", "kind: change"),
    "problem": ("Problem", "kind: problem"),
    "proposed": ("Proposed", "autopilot-staged: decomposed, awaiting release"),
}
_COLUMN_LABEL = {
    "backlog": "Backlog",
    "ready": "Ready",
    "doing": "In Progress",
    "review": "Review",
    "done": "Done",
}

_HTML_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>
:root{{--bg:#eef1f7;--panel:#fff;--panel2:#f6f8fc;--lane:#e8ecf5;--ink:#182031;
--ink2:#48546b;--ink3:#7a869e;--hair:#d7dded;--hair2:#e6eaf3;--accent:#268aa2;
--accentsoft:#d3ecf2;--crit:#d43a3f;--high:#c8781f;--med:#8a93a8;--low:#9aa4b8;
--incident:#e85a37;--change:#4f7fe0;--problem:#9160e0;--done:#2f9e6b;color-scheme:light;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0d1119;--panel:#151b28;--panel2:#121824;
--lane:#10151f;--ink:#e7ecf6;--ink2:#a7b2c8;--ink3:#6f7c95;--hair:#26303f;--hair2:#1d2532;
--accent:#4bb8d1;--accentsoft:#123039;--crit:#f0575c;--high:#e39a44;--med:#7f8aa2;--low:#626d84;
--incident:#ff7a54;--change:#6b96f2;--problem:#a97cf0;--done:#45c288;color-scheme:dark;}}}}
:root[data-theme="light"]{{--bg:#eef1f7;--panel:#fff;--panel2:#f6f8fc;--lane:#e8ecf5;--ink:#182031;
--ink2:#48546b;--ink3:#7a869e;--hair:#d7dded;--hair2:#e6eaf3;--accent:#268aa2;--accentsoft:#d3ecf2;
--crit:#d43a3f;--high:#c8781f;--med:#8a93a8;--low:#9aa4b8;--incident:#e85a37;--change:#4f7fe0;
--problem:#9160e0;--done:#2f9e6b;color-scheme:light;}}
:root[data-theme="dark"]{{--bg:#0d1119;--panel:#151b28;--panel2:#121824;--lane:#10151f;--ink:#e7ecf6;
--ink2:#a7b2c8;--ink3:#6f7c95;--hair:#26303f;--hair2:#1d2532;--accent:#4bb8d1;--accentsoft:#123039;
--crit:#f0575c;--high:#e39a44;--med:#7f8aa2;--low:#626d84;--incident:#ff7a54;--change:#6b96f2;
--problem:#a97cf0;--done:#45c288;color-scheme:dark;}}
*{{box-sizing:border-box;}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:14px;line-height:1.45;}}
.mono{{font-family:ui-monospace,"SF Mono","JetBrains Mono",monospace;font-variant-numeric:tabular-nums;}}
.wrap{{max-width:1500px;margin:0 auto;padding:22px 20px 48px;}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 18px;padding-bottom:16px;margin-bottom:16px;border-bottom:1px solid var(--hair);}}
h1{{font-size:19px;margin:0;font-weight:680;letter-spacing:-.01em;}}
.sub{{color:var(--ink3);font-size:12.5px;}}
.spacer{{flex:1 1 40px;}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;}}
.stat{{background:var(--panel);border:1px solid var(--hair);border-radius:9px;padding:6px 11px;display:flex;flex-direction:column;min-width:70px;}}
.stat .n{{font-size:16px;font-weight:700;}}
.stat .l{{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);}}
.board-scroll{{overflow-x:auto;padding-bottom:6px;}}
.board{{display:grid;grid-template-columns:148px repeat(5,minmax(216px,1fr));min-width:1180px;border:1px solid var(--hair);border-radius:14px;overflow:hidden;background:var(--panel2);}}
.corner{{background:var(--panel);border-bottom:1px solid var(--hair);border-right:1px solid var(--hair2);}}
.colhead{{background:var(--panel);border-bottom:1px solid var(--hair);border-right:1px solid var(--hair2);padding:11px 13px;display:flex;align-items:center;justify-content:space-between;gap:8px;}}
.colhead:last-child{{border-right:none;}}
.colhead .name{{font-size:12px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;}}
.colhead.donecol .name{{color:var(--done);}}
.wip{{font-size:11px;color:var(--ink3);border:1px solid var(--hair);border-radius:20px;padding:1px 8px;}}
.wip.over{{color:var(--crit);border-color:color-mix(in srgb,var(--crit) 45%,var(--hair));background:color-mix(in srgb,var(--crit) 10%,transparent);}}
.lanelabel{{border-right:1px solid var(--hair);border-bottom:1px solid var(--hair2);padding:14px 12px;background:var(--lane);display:flex;flex-direction:column;gap:6px;}}
.lname{{font-weight:640;font-size:12.5px;}}
.lkind{{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);font-family:ui-monospace,monospace;}}
.lanerow{{display:contents;}}
.cell{{border-right:1px solid var(--hair2);border-bottom:1px solid var(--hair2);padding:8px;display:flex;flex-direction:column;gap:8px;min-height:56px;}}
.cell:nth-child(6n){{border-right:none;}}
.cell.expedite{{background:color-mix(in srgb,var(--incident) 6%,transparent);}}
.card{{position:relative;background:var(--panel);border:1px solid var(--hair);border-radius:9px;padding:9px 10px 9px 12px;display:flex;flex-direction:column;gap:7px;transition:transform .12s ease,border-color .12s;}}
.card:hover{{transform:translateY(-1px);border-color:color-mix(in srgb,var(--accent) 40%,var(--hair));}}
.card::before{{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:3px;background:var(--stripe,var(--med));}}
.p-critical{{--stripe:var(--crit);}} .p-high{{--stripe:var(--high);}} .p-medium{{--stripe:var(--med);}} .p-low{{--stripe:var(--low);}}
.ctop{{display:flex;align-items:center;gap:6px;}}
.badge{{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;padding:2px 6px;border-radius:5px;}}
.badge.task{{color:var(--accent);background:var(--accentsoft);}}
.badge.epic{{color:#fff;background:var(--accent);}}
.badge.incident{{color:#fff;background:var(--incident);}}
.badge.change{{color:#fff;background:var(--change);}}
.badge.problem{{color:#fff;background:var(--problem);}}
.cid{{margin-left:auto;font-size:10.5px;color:var(--ink3);}}
.sev{{font-size:9.5px;font-weight:700;padding:1px 5px;border-radius:4px;color:#fff;}}
.ctitle{{font-size:12.7px;line-height:1.3;font-weight:520;text-wrap:pretty;}}
.cfoot{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}}
.owner{{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--ink2);}}
.ava{{width:17px;height:17px;border-radius:50%;display:grid;place-items:center;font-size:9px;font-weight:700;color:#fff;background:var(--accent);}}
.tag{{font-size:10px;color:var(--ink3);background:var(--panel2);border:1px solid var(--hair2);border-radius:5px;padding:1px 6px;font-family:ui-monospace,monospace;}}
.cell.done .card{{opacity:.82;}}
.note{{margin-top:22px;padding:14px 16px;border-radius:11px;background:var(--panel);border:1px solid var(--hair);color:var(--ink2);font-size:12.5px;line-height:1.55;}}
.note b{{color:var(--ink);}}
.card:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;}}
@media(prefers-reduced-motion:reduce){{.card{{transition:none;}}}}
</style></head><body><div class="wrap">
"""


def _sev_bg(sev: str) -> str:
    """Background var for a severity chip."""
    return "var(--incident)" if sev in ("sev1", "sev2") else "var(--med)"


def _clean(text: str) -> str:
    """Escape for HTML and normalize em/en dashes to a plain hyphen.

    Card titles come from live coord/ITIL data that may contain typographic
    dashes; the generated board keeps the house rule of plain hyphens only.
    """
    return html.escape(text).replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def _render_card(c: Card) -> str:
    """Render one card to escaped HTML."""
    stripe = f"p-{html.escape(c.priority)}"
    badge = f'<span class="badge {c.kind.value}">{c.kind.value}</span>'
    sev = ""
    sev_val = c.meta.get("severity")
    if sev_val:
        sev = f'<span class="sev" style="background:{_sev_bg(sev_val)}">{html.escape(str(sev_val)).upper()}</span>'  # noqa: E501
    cid = f'<span class="cid mono">#{html.escape(c.id)}</span>'
    title = f'<div class="ctitle">{_clean(c.title)}</div>'
    foot = ""
    if c.owner:
        initial = html.escape(c.owner[:1].upper())
        foot = (
            f'<div class="cfoot"><span class="owner">'
            f'<span class="ava">{initial}</span>{_clean(c.owner)}</span></div>'
        )
    elif c.labels:
        foot = f'<div class="cfoot"><span class="tag">{_clean(c.labels[0])}</span></div>'
    stripe_style = ""
    if c.kind == Kind.INCIDENT:
        stripe_style = ' style="--stripe:var(--incident)"'
    elif c.kind == Kind.CHANGE:
        stripe_style = ' style="--stripe:var(--change)"'
    elif c.kind == Kind.PROBLEM:
        stripe_style = ' style="--stripe:var(--problem)"'
    return (
        f'<div class="card {stripe}"{stripe_style} tabindex="0">'
        f'<div class="ctop">{badge}{sev}{cid}</div>{title}{foot}</div>'
    )


def render_html(kb: "KanbanBoard", title: str = "SKBoard") -> str:
    """Render the kanban board as a self-contained HTML document.

    The output styles both light and dark themes, HTML-escapes every dynamic
    string, and contains no em or en dashes.
    """
    grid = kb.grid()
    all_cards = kb.cards()
    active = len([c for c in all_cards if c.status != Column.DONE])
    done = len([c for c in all_cards if c.status == Column.DONE])
    itil_n = len([c for c in all_cards if c.source == "itil"])

    parts = [_HTML_HEAD.format(title=html.escape(title))]
    parts.append(
        "<header><div><h1>SKBoard</h1>"
        '<div class="sub mono">cards/ &middot; kind in {task, epic, incident, problem, change}</div></div>'  # noqa: E501
        '<div class="spacer"></div><div class="stats">'
        f'<div class="stat"><span class="n mono">{active}</span><span class="l">Active</span></div>'  # noqa: E501
        f'<div class="stat"><span class="n mono">{itil_n}</span><span class="l">ITIL</span></div>'
        f'<div class="stat"><span class="n mono">{done}</span><span class="l">Done</span></div>'
        "</div></header>"
    )

    wip = kb.wip_report()
    parts.append('<div class="board-scroll"><div class="board">')
    # column header row
    parts.append('<div class="corner"></div>')
    for col in COLUMN_ORDER:
        total = sum(len(grid[lane][col]) for lane in LANE_ORDER)
        donecls = " donecol" if col == "done" else ""
        limit = wip[col]["limit"]
        if limit is not None:
            label = f"{wip[col]['count']} / {limit}"
            overcls = " over" if wip[col]["over"] else ""
        else:
            label = str(total)
            overcls = ""
        parts.append(
            f'<div class="colhead{donecls}"><span class="name">{_COLUMN_LABEL[col]}</span>'
            f'<span class="wip mono{overcls}">{label}</span></div>'
        )
    # lane rows (skip empty lanes to keep the board tight)
    for lane in LANE_ORDER:
        lane_cards = sum(len(grid[lane][col]) for col in COLUMN_ORDER)
        if lane_cards == 0:
            continue
        name, kind_label = _LANE_META[lane]
        parts.append('<div class="lanerow">')
        parts.append(
            f'<div class="lanelabel"><span class="lname">{name}</span>'
            f'<span class="lkind">{kind_label}</span></div>'
        )
        for col in COLUMN_ORDER:
            expe = " expedite" if lane == "expedite" else ""
            donecls = " done" if col == "done" else ""
            cards_html = "".join(_render_card(c) for c in grid[lane][col])
            parts.append(f'<div class="cell{expe}{donecls}">{cards_html}</div>')
        parts.append("</div>")
    parts.append("</div></div>")

    parts.append(
        '<div class="note"><b>Projection of live data.</b> Columns are the shared '
        "lifecycle, swimlanes are the card kind. The Expedite lane carries incidents. "
        "This board, BOARD.md, and the JSON view are all projections of one fold, so "
        "they cannot drift.</div>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)
