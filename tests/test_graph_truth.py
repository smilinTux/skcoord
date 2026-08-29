"""Joined graph truth, write verification, and unenforced-fence audit tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

import skcoord.card_store as card_store_module
from skcoord.card import CardEvent, CardEventLog
from skcoord.card_store import CardCore, CardStore
from skcoord.graph_truth import (
    audit_graph_truth,
    read_joined_truth,
    write_verified_annotation,
)


def _card(
    home: Path,
    card_id: str,
    *,
    dependencies: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
) -> None:
    """Create one authoritative fixture card."""
    CardStore(home).create(
        CardCore(
            id=card_id,
            title=card_id,
            dependencies=list(dependencies),
            initial_labels=list(labels),
        )
    )


def _legacy_link(
    home: Path,
    card_id: str,
    key: str,
    value: str,
    *,
    writer: str = "fixture",
    ts: str = "2026-01-01T00:00:00+00:00",
    seq: int = 0,
) -> None:
    """Append one legacy-only fixture link."""
    CardEventLog(home).append(
        CardEvent(
            card_id=card_id,
            action="link",
            writer=writer,
            ts=ts,
            seq=seq,
            link_key=key,
            link_value=value,
        )
    )


def _raw_legacy_file(home: Path, name: str, *events: CardEvent) -> None:
    """Write a fixture writer file with a chosen enumeration position."""
    directory = home / "coordination" / "card_events"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "".join(event.model_dump_json() + "\n" for event in events),
        encoding="utf-8",
    )


def _snapshot(home: Path) -> dict[str, tuple]:
    """Capture every filesystem node, including metadata and link targets."""
    if not home.exists():
        return {}
    snapshot: dict[str, tuple] = {}
    for directory, names, filenames in os.walk(home, topdown=True, followlinks=False):
        parent = Path(directory)
        for name in sorted((*names, *filenames)):
            path = parent / name
            relative = str(path.relative_to(home))
            info = path.lstat()
            common = (stat.S_IFMT(info.st_mode), info.st_mode, info.st_size, info.st_mtime_ns)
            if stat.S_ISREG(info.st_mode):
                snapshot[relative] = (*common, hashlib.sha256(path.read_bytes()).hexdigest())
            elif stat.S_ISLNK(info.st_mode):
                snapshot[relative] = (*common, os.readlink(path))
            else:
                snapshot[relative] = common
    return snapshot


@pytest.mark.parametrize("verdict", ["BLOCKED", "PASS"])
def test_complete_keeps_only_explicit_verdict_evidence(tmp_path: Path, verdict: str) -> None:
    """DONE remains lifecycle and never synthesizes or overrides a verdict."""
    _card(tmp_path, "review01")
    CardStore(tmp_path).append_event("review01", "complete", "fixture")
    write_verified_annotation(
        tmp_path,
        "review01",
        "link",
        "reviewer",
        link_key="verdict",
        link_value=verdict,
    )

    truth = read_joined_truth(tmp_path, "review01")

    assert truth.lifecycle == "done"
    assert [(item.key, item.value) for item in truth.verdicts] == [("verdict", verdict)]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("disposition", "BLOCKED"),
        ("result", "FAIL_CLOSED"),
        ("closure-state", "PASS_EVIDENCE_COMPLETE"),
        ("independent_review", "980fb58e:PASS:reviewed"),
        ("gate_status", "APPROVAL_RECORDED_BUT_BLOCKED"),
        ("review", "exact-base review PASS with evidence"),
        ("review-result", "CHANGES_REQUIRED"),
        ("review_status", "HOLD"),
    ],
)
def test_actual_legacy_verdict_shapes_are_explicit(tmp_path: Path, key: str, value: str) -> None:
    """Recognize current board keys only when their value has a verdict token."""
    _card(tmp_path, "verdict1")
    _legacy_link(tmp_path, "verdict1", key, value)

    truth = read_joined_truth(tmp_path, "verdict1")

    assert [(item.key, item.value) for item in truth.verdicts] == [(key, value)]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("review", "evidence attached and independently inspected"),
        ("review-result", "complete"),
        ("review-status", "done"),
        ("lifecycle", "PASS"),
        ("status", "DONE"),
    ],
)
def test_vague_prose_and_lifecycle_never_become_verdicts(
    tmp_path: Path, key: str, value: str
) -> None:
    """A recognized key and explicit verdict token are both required."""
    _card(tmp_path, "verdict2")
    _legacy_link(tmp_path, "verdict2", key, value)

    assert read_joined_truth(tmp_path, "verdict2").verdicts == []


def test_done_without_verdict_does_not_mean_pass(tmp_path: Path) -> None:
    """Lifecycle completion alone carries no review result."""
    _card(tmp_path, "review02")
    CardStore(tmp_path).append_event("review02", "complete", "fixture")

    assert read_joined_truth(tmp_path, "review02").verdicts == []


def test_legacy_links_and_labels_fold_by_global_event_order(tmp_path: Path) -> None:
    """File enumeration cannot make an older writer event win."""
    _card(tmp_path, "ordering", labels=("initial",))
    older = "2020-01-01T00:00:00+00:00"
    newer = "2030-01-01T00:00:00+00:00"
    _raw_legacy_file(
        tmp_path,
        "a.jsonl",
        CardEvent(
            card_id="ordering",
            action="link",
            writer="new",
            ts=newer,
            link_key="verdict",
            link_value="PASS",
        ),
        CardEvent(
            card_id="ordering",
            action="remove_label",
            writer="new",
            ts=newer,
            label="legacy-label",
        ),
    )
    _raw_legacy_file(
        tmp_path,
        "z.jsonl",
        CardEvent(
            card_id="ordering",
            action="link",
            writer="old",
            ts=older,
            link_key="verdict",
            link_value="BLOCKED",
        ),
        CardEvent(
            card_id="ordering",
            action="add_label",
            writer="old",
            ts=older,
            label="legacy-label",
        ),
    )

    truth = read_joined_truth(tmp_path, "ordering")

    assert [(item.key, item.value) for item in truth.annotations] == [("verdict", "PASS")]
    assert truth.labels == ["initial"]
    provenance = {item.label: item for item in truth.label_provenance}
    assert provenance["initial"].authoritative
    assert provenance["legacy-label"].legacy_removed
    assert not provenance["legacy-label"].legacy


def test_label_provenance_preserves_cross_store_disagreement_and_removals(
    tmp_path: Path,
) -> None:
    """The current union never silently collapses independent store states."""
    _card(tmp_path, "labels01", labels=("stale", "auth-only"))
    CardStore(tmp_path).append_event("labels01", "remove_label", "authoritative", label="stale")
    CardStore(tmp_path).append_event("labels01", "add_label", "authoritative", label="auth-added")
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="labels01",
            action="add_label",
            writer="legacy",
            label="stale",
        )
    )
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="labels01",
            action="remove_label",
            writer="legacy",
            label="legacy-removed",
        )
    )

    truth = read_joined_truth(tmp_path, "labels01")
    state = {item.label: item.model_dump() for item in truth.label_provenance}

    assert truth.labels == ["auth-added", "auth-only", "stale"]
    assert state["stale"] == {
        "label": "stale",
        "authoritative": False,
        "legacy": True,
        "authoritative_removed": True,
        "legacy_removed": False,
    }
    assert state["auth-only"]["authoritative"] is True
    assert state["legacy-removed"]["legacy_removed"] is True
    assert truth.model_dump_json() == read_joined_truth(tmp_path, "labels01").model_dump_json()


def test_link_provenance_preserves_current_store_disagreement(tmp_path: Path) -> None:
    """Different current values remain separate records with exact store flags."""
    _card(tmp_path, "links001")
    CardStore(tmp_path).append_event(
        "links001",
        "link",
        "authoritative",
        link_key="result",
        link_value="PASS",
    )
    _legacy_link(tmp_path, "links001", "result", "BLOCKED")

    truth = read_joined_truth(tmp_path, "links001")

    assert [item.model_dump() for item in truth.annotations] == [
        {
            "key": "result",
            "value": "BLOCKED",
            "authoritative": False,
            "legacy": True,
        },
        {
            "key": "result",
            "value": "PASS",
            "authoritative": True,
            "legacy": False,
        },
    ]
    assert truth.dependencies == []


def test_incident_fixtures_include_claimed_card_with_unrelated_dependencies(
    tmp_path: Path,
) -> None:
    """Cover the exact false-cycle and stale-execution incident shapes."""
    for card_id in (
        "19acf874",
        "2dc0c14d",
        "3dca587d",
        "645d53d4",
        "781a2f17",
        "95e192fd",
    ):
        _card(tmp_path, card_id)
    _card(
        tmp_path,
        "2a9fad93",
        dependencies=("19acf874", "2dc0c14d", "3dca587d"),
    )
    CardStore(tmp_path).append_event("2a9fad93", "claim", "fixture", owner="live-owner")
    _card(tmp_path, "f1e3e96b", dependencies=("781a2f17",))
    _card(tmp_path, "5a14e4e8")
    _legacy_link(tmp_path, "2a9fad93", "custody_owner_gate", "645d53d4")
    _legacy_link(tmp_path, "645d53d4", "dependency-cycle", "2a9fad93")
    _legacy_link(
        tmp_path,
        "5a14e4e8",
        "stale-execution-blocked",
        "f1e3e96b|new dependency=781a2f17|old candidate must not execute",
    )
    _legacy_link(tmp_path, "f1e3e96b", "dependency", "781a2f17")
    _legacy_link(tmp_path, "95e192fd", "verdict", "BLOCKED")

    before = _snapshot(tmp_path)
    report = audit_graph_truth(tmp_path)

    unenforced = {
        (item.card_id, item.key)
        for item in report.findings
        if item.code == "unenforced_annotation"
    }
    assert ("2a9fad93", "custody_owner_gate") in unenforced
    assert ("645d53d4", "dependency-cycle") in unenforced
    assert ("5a14e4e8", "stale-execution-blocked") in unenforced
    assert ("f1e3e96b", "dependency") not in unenforced
    assert any(
        item.card_id == "95e192fd" and item.code == "legacy_only_verdict_evidence"
        for item in report.findings
    )
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "mechanism",
    ["dependency", "claim", "void", "archive", "label"],
)
def test_unrelated_mechanisms_do_not_suppress_a_false_fence(
    tmp_path: Path, mechanism: str
) -> None:
    """Reproduce every predecessor card-global suppression mechanism."""
    _card(tmp_path, "target01")
    _card(tmp_path, "other001")
    if mechanism == "dependency":
        CardStore(tmp_path).append_event(
            "target01", "add_dependency", "fixture", dependency="other001"
        )
    elif mechanism == "claim":
        CardStore(tmp_path).append_event("target01", "claim", "fixture", owner="unrelated-owner")
    elif mechanism == "void":
        CardStore(tmp_path).append_event("target01", "void", "fixture")
    elif mechanism == "archive":
        CardStore(tmp_path).append_event("target01", "archive", "fixture")
    else:
        CardStore(tmp_path).append_event("target01", "add_label", "fixture", label="human-gate")
    _legacy_link(tmp_path, "target01", "unrelated-blocker", "must not execute")

    report = audit_graph_truth(tmp_path)

    assert any(
        item.card_id == "target01"
        and item.key == "unrelated-blocker"
        and item.code == "unenforced_annotation"
        for item in report.findings
    )


def test_annotation_specific_claim_label_void_and_archive_can_enforce(
    tmp_path: Path,
) -> None:
    """Exact mechanism assertions still match their own current state."""
    fixtures = {
        "claim001": ("claim-gate", "active claim blocks execution", "claim"),
        "label001": ("human-gate", "human-gate", "label"),
        "void0001": ("void-gate", "voided card must not execute", "void"),
        "archive1": ("archive-gate", "archived card must not execute", "archive"),
    }
    for card_id, (key, value, mechanism) in fixtures.items():
        _card(tmp_path, card_id)
        if mechanism == "claim":
            CardStore(tmp_path).append_event(card_id, "claim", "fixture", owner="owner")
        elif mechanism == "label":
            CardStore(tmp_path).append_event(card_id, "add_label", "fixture", label="human-gate")
        else:
            CardStore(tmp_path).append_event(card_id, mechanism, "fixture")
        _legacy_link(tmp_path, card_id, key, value)

    report = audit_graph_truth(tmp_path)

    assert not [item for item in report.findings if item.code == "unenforced_annotation"]


def test_bounded_audit_has_complete_counts_and_stable_serialization(
    tmp_path: Path,
) -> None:
    """Truncation never changes total populations or deterministic ordering."""
    for card_id in ("bound001", "bound002", "bound003"):
        _card(tmp_path, card_id)
        _legacy_link(tmp_path, card_id, "execution-blocked", "must not execute")
    CardStore(tmp_path).append_event(
        "bound001", "link", "fixture", link_key="result", link_value="PASS"
    )

    small = audit_graph_truth(tmp_path, limit=1)
    repeated = audit_graph_truth(tmp_path, limit=1)
    full = audit_graph_truth(tmp_path, limit=2000)

    assert small.model_dump_json() == repeated.model_dump_json()
    assert small.truncated and len(small.findings) == 1
    assert small.total_findings == full.total_findings == 4
    assert (
        small.population_counts
        == full.population_counts
        == {
            "unenforced_annotations": 3,
            "legacy_only_verdict_evidence": 0,
            "authoritative_only_verdict_evidence": 1,
        }
    )
    assert full.findings == sorted(
        full.findings,
        key=lambda item: (item.card_id, item.code, item.key, item.value),
    )


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("add_label", {"label": "reviewed"}),
        ("link", {"link_key": "verdict", "link_value": "PASS"}),
    ],
)
def test_verified_annotation_has_exact_two_store_identity(
    tmp_path: Path, action: str, payload: dict[str, str]
) -> None:
    """Success requires the same exact event identity in both projections."""
    _card(tmp_path, "write001")

    event = write_verified_annotation(
        tmp_path,
        "write001",
        action,
        "writer",
        transition_id=f"operation-{action}",
        **payload,
    )

    raw = CardStore(tmp_path)._read_events("write001")
    assert [item["event_id"] for item in raw] == [event["event_id"]]
    overlay = [item for item in CardEventLog(tmp_path).read_all() if item.card_id == "write001"]
    assert len(overlay) == 1
    assert overlay[0].event_id == event["event_id"]
    assert overlay[0].writer == "writer"


def test_old_overlay_events_without_identity_remain_readable(tmp_path: Path) -> None:
    """The optional identity addition is backward compatible."""
    _card(tmp_path, "old00001")
    _raw_legacy_file(
        tmp_path,
        "old.jsonl",
        CardEvent(
            card_id="old00001",
            action="link",
            writer="old-writer",
            link_key="verdict",
            link_value="BLOCKED",
        ),
    )

    raw = tmp_path / "coordination" / "card_events" / "old.jsonl"
    document = json.loads(raw.read_text(encoding="utf-8"))
    document.pop("event_id")
    raw.write_text(json.dumps(document) + "\n", encoding="utf-8")

    events = CardEventLog(tmp_path).read_all()
    assert events[0].event_id is None
    assert read_joined_truth(tmp_path, "old00001").verdicts[0].legacy


def test_meaningful_label_removal_is_verified_in_both_stores(tmp_path: Path) -> None:
    """Removal starts from a materially present authoritative and legacy label."""
    _card(tmp_path, "remove01", labels=("reviewed",))
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="remove01",
            action="add_label",
            writer="fixture",
            label="reviewed",
        )
    )

    write_verified_annotation(
        tmp_path,
        "remove01",
        "remove_label",
        "writer",
        label="reviewed",
        transition_id="remove-reviewed",
    )

    truth = read_joined_truth(tmp_path, "remove01")
    provenance = truth.label_provenance[0]
    assert truth.labels == []
    assert provenance.authoritative_removed
    assert provenance.legacy_removed


def test_mutation_lock_covers_writes_and_independent_readbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every protocol I/O occurs while the existing per-card lock is held."""
    _card(tmp_path, "locked01")
    original_lock = card_store_module.card_mutation_lock
    original_store_read = CardStore._read_events
    original_overlay_read = CardEventLog.read_all
    original_overlay_append = CardEventLog.append
    held = False
    observations: list[tuple[str, bool]] = []

    @contextmanager
    def observed_lock(home, card_id, timeout_seconds=5.0, **kwargs):
        nonlocal held
        with original_lock(home, card_id, timeout_seconds, **kwargs):
            held = True
            try:
                yield
            finally:
                held = False

    def store_read(self, card_id):
        observations.append(("store-read", held))
        return original_store_read(self, card_id)

    def overlay_read(self):
        observations.append(("overlay-read", held))
        return original_overlay_read(self)

    def overlay_append(self, event):
        observations.append(("overlay-append", held))
        return original_overlay_append(self, event)

    monkeypatch.setattr(card_store_module, "card_mutation_lock", observed_lock)
    monkeypatch.setattr(CardStore, "_read_events", store_read)
    monkeypatch.setattr(CardEventLog, "read_all", overlay_read)
    monkeypatch.setattr(CardEventLog, "append", overlay_append)

    write_verified_annotation(
        tmp_path,
        "locked01",
        "link",
        "writer",
        link_key="result",
        link_value="PASS",
    )

    # Only the read-only foldable-core preflight may run before lock creation.
    # Every protocol operation after lock acquisition remains in the section.
    first_locked = next(index for index, (_, held) in enumerate(observations) if held)
    assert observations[:first_locked]
    assert all(
        kind in {"store-read", "overlay-read"} and not held
        for kind, held in observations[:first_locked]
    )
    assert all(held for _, held in observations[first_locked:])


def test_concurrent_identical_retry_is_idempotent(tmp_path: Path) -> None:
    """Same-card writers with one operation token converge to one exact pair."""
    _card(tmp_path, "retry001")
    barrier = threading.Barrier(2)

    def write():
        barrier.wait()
        return write_verified_annotation(
            tmp_path,
            "retry001",
            "link",
            "writer",
            link_key="result",
            link_value="PASS",
            transition_id="stable-retry",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        events = list(pool.map(lambda _index: write(), range(2)))

    assert events[0]["event_id"] == events[1]["event_id"]
    assert len(CardStore(tmp_path)._read_events("retry001")) == 1
    assert (
        len(
            [
                event
                for event in CardEventLog(tmp_path).read_all()
                if event.event_id == events[0]["event_id"]
            ]
        )
        == 1
    )


def test_different_card_mutations_can_progress_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-card locking does not become an accidental board-global lock."""
    _card(tmp_path, "parallel1")
    _card(tmp_path, "parallel2")
    original = CardEventLog.append
    entered = threading.Barrier(2)

    def synchronized_append(self, event):
        entered.wait(timeout=2)
        return original(self, event)

    monkeypatch.setattr(CardEventLog, "append", synchronized_append)

    def write(card_id):
        return write_verified_annotation(
            tmp_path,
            card_id,
            "link",
            "writer",
            link_key="result",
            link_value="PASS",
            transition_id=f"transition-{card_id}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("parallel1", "parallel2")))

    assert len(results) == 2


def test_retry_repairs_authoritative_only_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stable operation token repairs partial state without duplicate authority."""
    _card(tmp_path, "partial1")
    original = CardEventLog.append

    def fail_append(_self, _event):
        raise OSError("overlay unavailable")

    monkeypatch.setattr(CardEventLog, "append", fail_append)
    with pytest.raises(RuntimeError, match="partial state reported"):
        write_verified_annotation(
            tmp_path,
            "partial1",
            "link",
            "writer",
            link_key="result",
            link_value="PASS",
            transition_id="repair-partial",
        )
    monkeypatch.setattr(CardEventLog, "append", original)

    event = write_verified_annotation(
        tmp_path,
        "partial1",
        "link",
        "writer",
        link_key="result",
        link_value="PASS",
        transition_id="repair-partial",
    )

    assert len(CardStore(tmp_path)._read_events("partial1")) == 1
    assert [item.event_id for item in CardEventLog(tmp_path).read_all()] == [event["event_id"]]


def test_wrong_writer_overlay_substitution_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching payload and identity cannot substitute a different writer."""
    _card(tmp_path, "wrong001")
    original = CardEventLog.append

    def substitute(self, event):
        return original(self, event.model_copy(update={"writer": "other-writer"}))

    monkeypatch.setattr(CardEventLog, "append", substitute)

    with pytest.raises(RuntimeError, match="partial state reported"):
        write_verified_annotation(
            tmp_path,
            "wrong001",
            "link",
            "intended-writer",
            link_key="result",
            link_value="PASS",
            transition_id="wrong-writer",
        )
    overlay = CardEventLog(tmp_path).read_all()[0]
    assert overlay.writer == "other-writer"


def test_wrong_overlay_identity_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An otherwise exact event with a substitute identity is rejected."""
    _card(tmp_path, "wrong002")
    original = CardEventLog.append

    def substitute(self, event):
        return original(self, event.model_copy(update={"event_id": "substitute-id"}))

    monkeypatch.setattr(CardEventLog, "append", substitute)

    with pytest.raises(RuntimeError, match="partial state reported"):
        write_verified_annotation(
            tmp_path,
            "wrong002",
            "link",
            "writer",
            link_key="result",
            link_value="PASS",
            transition_id="wrong-identity",
        )


def test_overlay_readback_failure_reports_authoritative_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An independently unreadable overlay event can never be success."""
    _card(tmp_path, "write002")
    original = CardEventLog.read_all

    def hide_identified_events(self):
        return [event for event in original(self) if event.event_id is None]

    monkeypatch.setattr(CardEventLog, "read_all", hide_identified_events)
    with pytest.raises(RuntimeError, match="partial state reported"):
        write_verified_annotation(
            tmp_path,
            "write002",
            "link",
            "writer",
            link_key="verdict",
            link_value="PASS",
        )
    assert CardStore(tmp_path)._read_events("write002")[0]["action"] == "link"


def test_authoritative_readback_failure_never_reaches_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable write with failed independent readback is reported as failure."""
    _card(tmp_path, "write003")
    original = CardStore._read_events

    def lose_verified_events(self, card_id):
        return [event for event in original(self, card_id) if "transition_id" not in event]

    monkeypatch.setattr(CardStore, "_read_events", lose_verified_events)
    with pytest.raises(RuntimeError, match="authoritative readback failed"):
        write_verified_annotation(
            tmp_path,
            "write003",
            "link",
            "writer",
            link_key="verdict",
            link_value="PASS",
        )
    assert CardEventLog(tmp_path).read_all() == []
    assert original(CardStore(tmp_path), "write003")[0]["action"] == "link"


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("link", {"link_key": "result", "link_value": "PASS"}),
        ("add_label", {"label": "reviewed"}),
        ("remove_label", {"label": "reviewed"}),
    ],
)
def test_unknown_card_is_rejected_before_any_annotation_write(
    tmp_path: Path, action: str, payload: dict[str, str]
) -> None:
    """Unknown cards are rejected before lock paths or events are created."""
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="no foldable core"):
        write_verified_annotation(
            tmp_path,
            "unknown1",
            action,
            "writer",
            **payload,
        )

    assert _snapshot(tmp_path) == before
    assert CardStore(tmp_path).list_card_ids() == []
    assert CardEventLog(tmp_path).read_all() == []


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("link", {"link_key": "result", "link_value": "PASS"}),
        ("add_label", {"label": "reviewed"}),
        ("remove_label", {"label": "reviewed"}),
    ],
)
def test_concurrent_unknown_card_rejections_are_storage_neutral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    payload: dict[str, str],
) -> None:
    """Concurrent stable-unknown calls never enter either lock implementation."""
    original = card_store_module.card_mutation_lock
    entries = 0
    guard = threading.Lock()

    @contextmanager
    def observed_lock(*args, **kwargs):
        nonlocal entries
        with guard:
            entries += 1
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(card_store_module, "card_mutation_lock", observed_lock)
    before = _snapshot(tmp_path)
    barrier = threading.Barrier(16)

    def reject(_index: int) -> str:
        barrier.wait()
        with pytest.raises(ValueError, match="no foldable core"):
            write_verified_annotation(tmp_path, "unknown1", action, "writer", **payload)
        return "rejected"

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(reject, range(16))) == ["rejected"] * 16

    assert entries == 0
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("link", {"link_key": "result", "link_value": "PASS"}),
        ("add_label", {"label": "reviewed"}),
        ("remove_label", {"label": "reviewed"}),
    ],
)
@pytest.mark.parametrize("race", ["loss", "malformation"])
def test_raced_invalid_core_rejection_creates_no_helper_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    payload: dict[str, str],
    race: str,
) -> None:
    """Force core invalidation exactly after preflight and before lock entry."""
    _card(tmp_path, "race0001")
    original = card_store_module.card_mutation_lock
    core = tmp_path / "cards" / "race0001" / "core.json"
    boundary_reached = False
    expected: dict[str, tuple] = {}

    @contextmanager
    def invalidate_then_lock(home, card_id, timeout_seconds=5.0, **kwargs):
        nonlocal boundary_reached, expected
        boundary_reached = True
        if race == "loss":
            core.unlink()
        else:
            core.write_text("{malformed", encoding="utf-8")
        expected = _snapshot(tmp_path)
        with original(home, card_id, timeout_seconds, **kwargs):
            yield

    monkeypatch.setattr(card_store_module, "card_mutation_lock", invalidate_then_lock)

    with pytest.raises(ValueError, match="no foldable core|malformed"):
        write_verified_annotation(tmp_path, "race0001", action, "writer", **payload)

    assert boundary_reached
    assert _snapshot(tmp_path) == expected
    assert not (tmp_path / "cards" / "race0001" / "events").exists()


def test_replaced_core_cannot_split_helper_from_real_ordinary_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-byte atomic core replacement cannot split the card-ID anchor."""
    _card(tmp_path, "replace1")
    core = tmp_path / "cards" / "replace1" / "core.json"
    original_append = CardStore.append_event
    helper_inside = threading.Event()
    release_helper = threading.Event()
    ordinary_entered = threading.Event()

    def paused_append(self, card_id, action, agent, **payload):
        if agent == "helper":
            helper_inside.set()
            assert release_helper.wait(timeout=2)
        return original_append(self, card_id, action, agent, **payload)

    monkeypatch.setattr(CardStore, "append_event", paused_append)

    def helper_write() -> None:
        write_verified_annotation(
            tmp_path,
            "replace1",
            "link",
            "helper",
            link_key="result",
            link_value="PASS",
            transition_id="replacement-helper",
        )

    def ordinary_write() -> None:
        assert helper_inside.wait(timeout=2)
        replacement = core.with_name("replacement.tmp")
        replacement.write_bytes(core.read_bytes())
        os.replace(replacement, core)
        with card_store_module.card_mutation_lock(tmp_path, "replace1"):
            ordinary_entered.set()
            original_append(
                CardStore(tmp_path),
                "replace1",
                "note",
                "ordinary",
                text="serialized",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        helper = pool.submit(helper_write)
        ordinary = pool.submit(ordinary_write)
        assert helper_inside.wait(timeout=2)
        assert not ordinary_entered.wait(timeout=0.1)
        release_helper.set()
        helper.result(timeout=2)
        ordinary.result(timeout=2)

    assert ordinary_entered.is_set()
    assert [event["writer"] for event in CardStore(tmp_path)._read_events("replace1")] == [
        "helper",
        "ordinary",
    ]


def test_direct_append_event_uses_same_anchor_after_core_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain CardStore writer cannot bypass the helper's acquired anchor."""
    _card(tmp_path, "replace3")
    with card_store_module.card_mutation_lock(tmp_path, "replace3"):
        pass
    core = tmp_path / "cards" / "replace3" / "core.json"
    original_require = CardStore._require_foldable_core
    helper_inside = threading.Event()
    release_helper = threading.Event()
    ordinary_validated = threading.Event()

    def observed_require(self, card_id):
        if card_id == "replace3" and threading.current_thread().name == "ordinary-writer":
            ordinary_validated.set()
        return original_require(self, card_id)

    monkeypatch.setattr(CardStore, "_require_foldable_core", observed_require)

    def helper() -> None:
        with card_store_module.card_mutation_lock(tmp_path, "replace3", artifact_neutral=True):
            helper_inside.set()
            assert release_helper.wait(timeout=2)

    def ordinary() -> None:
        assert helper_inside.wait(timeout=2)
        replacement = core.with_name("replacement.tmp")
        replacement.write_bytes(core.read_bytes())
        os.replace(replacement, core)
        threading.current_thread().name = "ordinary-writer"
        CardStore(tmp_path).append_event("replace3", "note", "ordinary", text="serialized")

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(helper)
        writer = pool.submit(ordinary)
        assert helper_inside.wait(timeout=2)
        assert not ordinary_validated.wait(timeout=0.1)
        release_helper.set()
        held.result(timeout=2)
        writer.result(timeout=2)

    assert ordinary_validated.is_set()
    assert CardStore(tmp_path)._read_events("replace3")[0]["writer"] == "ordinary"


def test_card_directory_replacement_while_waiting_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiter never enters on a detached card-directory anchor."""
    _card(tmp_path, "replace2")
    with card_store_module.card_mutation_lock(tmp_path, "replace2"):
        pass
    card = tmp_path / "cards" / "replace2"
    old_card = tmp_path / "cards" / "replace2-old"
    original_open = card_store_module._open_existing_card_lock
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_opened = threading.Event()
    waiter_ident = 0
    waiter_entered = threading.Event()

    def observed_open(home, card_id):
        descriptor = original_open(home, card_id)
        if threading.get_ident() == waiter_ident and not waiter_opened.is_set():
            waiter_opened.set()
        return descriptor

    monkeypatch.setattr(card_store_module, "_open_existing_card_lock", observed_open)

    def holder() -> None:
        with card_store_module.card_mutation_lock(tmp_path, "replace2", artifact_neutral=True):
            holder_entered.set()
            assert release_holder.wait(timeout=2)

    def waiter() -> None:
        nonlocal waiter_ident
        waiter_ident = threading.get_ident()
        assert holder_entered.wait(timeout=2)
        with pytest.raises(ValueError, match="anchor changed"):
            with card_store_module.card_mutation_lock(tmp_path, "replace2", artifact_neutral=True):
                waiter_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(holder)
        waiting = pool.submit(waiter)
        assert holder_entered.wait(timeout=2)
        assert waiter_opened.wait(timeout=2)
        card.rename(old_card)
        card.mkdir()
        (card / "core.json").write_bytes((old_card / "core.json").read_bytes())
        release_holder.set()
        held.result(timeout=2)
        waiting.result(timeout=2)

    assert not waiter_entered.is_set()
    assert not (card / "events").exists()


def test_artifact_neutral_and_normal_locks_share_the_card_anchor(tmp_path: Path) -> None:
    """The common helper lock serializes with ordinary same-card mutations."""
    _card(tmp_path, "anchor01")
    with card_store_module.card_mutation_lock(tmp_path, "anchor01"):
        pass
    artifact_entered = threading.Event()
    release_artifact = threading.Event()
    normal_entered = threading.Event()

    def hold_artifact_lock() -> None:
        with card_store_module.card_mutation_lock(tmp_path, "anchor01", artifact_neutral=True):
            artifact_entered.set()
            assert release_artifact.wait(timeout=2)

    def enter_normal_lock() -> None:
        assert artifact_entered.wait(timeout=2)
        with card_store_module.card_mutation_lock(tmp_path, "anchor01"):
            normal_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        artifact = pool.submit(hold_artifact_lock)
        normal = pool.submit(enter_normal_lock)
        assert artifact_entered.wait(timeout=2)
        assert not normal_entered.wait(timeout=0.05)
        release_artifact.set()
        artifact.result(timeout=2)
        normal.result(timeout=2)

    assert normal_entered.is_set()


def test_artifact_neutral_lock_honors_preexisting_card_lock(tmp_path: Path) -> None:
    """A pre-existing persistent lock is honored but never deleted or changed."""
    _card(tmp_path, "anchor02")
    with card_store_module.card_mutation_lock(tmp_path, "anchor02"):
        pass
    before = _snapshot(tmp_path)
    persistent_entered = threading.Event()
    release_persistent = threading.Event()
    neutral_entered = threading.Event()

    def hold_persistent_lock() -> None:
        with card_store_module.card_mutation_lock(tmp_path, "anchor02"):
            persistent_entered.set()
            assert release_persistent.wait(timeout=2)

    def enter_neutral_lock() -> None:
        assert persistent_entered.wait(timeout=2)
        with card_store_module.card_mutation_lock(tmp_path, "anchor02", artifact_neutral=True):
            neutral_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        persistent = pool.submit(hold_persistent_lock)
        neutral = pool.submit(enter_neutral_lock)
        assert persistent_entered.wait(timeout=2)
        assert not neutral_entered.wait(timeout=0.05)
        release_persistent.set()
        persistent.result(timeout=2)
        neutral.result(timeout=2)

    assert neutral_entered.is_set()
    assert _snapshot(tmp_path) == before


def test_same_card_protocol_sections_do_not_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicting same-card mutations are serialized across both stores."""
    _card(tmp_path, "serial01")
    original = CardEventLog.append
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    def slow_append(self, event):
        nonlocal active, maximum
        with state_lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        try:
            return original(self, event)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(CardEventLog, "append", slow_append)

    def write(value):
        return write_verified_annotation(
            tmp_path,
            "serial01",
            "link",
            value,
            link_key="result",
            link_value=value,
            transition_id=f"serial-{value}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, ("PASS", "BLOCKED")))

    assert maximum == 1
    assert len(CardStore(tmp_path)._read_events("serial01")) == 2
