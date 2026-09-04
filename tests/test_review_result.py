"""Focused transactional review result tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.review_result import record_review_result


def _fixture(tmp_path: Path):
    store = CardStore(tmp_path)
    store.ensure_dirs()
    store.create(
        CardCore(
            id="parent01",
            kind="task",
            title="candidate",
            description="",
            created_by="producer",
        )
    )
    store.create(
        CardCore(
            id="review01",
            kind="task",
            title="review",
            description="",
            created_by="opener",
            initial_labels=["review", "parent-parent01"],
        )
    )
    store.append_event(
        "review01", "claim", "reviewer", owner="reviewer", claim_revision="review-r1"
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"result":"checked"}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    args = dict(
        review_card_id="review01",
        parent_card_id="parent01",
        reviewer_identity="reviewer",
        claim_revision="review-r1",
        verdict="PASS",
        evidence_uri=evidence.as_uri(),
        evidence_sha256=digest,
        notify=False,
    )
    return store, args


def test_records_one_native_event_and_terminal_state(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)

    receipt = record_review_result(tmp_path, **args)

    results = [e for e in store._read_events("review01") if e["action"] == "review_result"]
    assert len(results) == 1
    assert results[0]["event_id"] == receipt.event_id
    assert results[0]["verdict"] == "PASS"
    assert results[0]["evidence_sha256"] == args["evidence_sha256"]
    card = store.fold("review01")
    assert card.status.value == "done"
    assert card.owner is None
    assert card.meta["review_result"] == results[0]


def test_exact_replay_is_stable(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)
    first = record_review_result(tmp_path, **args)
    second = record_review_result(tmp_path, **args)
    assert second.replayed is True
    assert second.event_id == first.event_id
    assert len([e for e in store._read_events("review01") if e["action"] == "review_result"]) == 1


def test_stale_claim_has_no_partial_event(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)
    args["claim_revision"] = "stale"
    with pytest.raises(ValueError, match="stale claim"):
        record_review_result(tmp_path, **args)
    assert not [e for e in store._read_events("review01") if e["action"] == "review_result"]


def test_missing_evidence_has_no_partial_event(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)
    args["evidence_uri"] = (tmp_path / "missing.json").as_uri()
    with pytest.raises(ValueError, match="does not exist"):
        record_review_result(tmp_path, **args)
    assert not [e for e in store._read_events("review01") if e["action"] == "review_result"]


def test_blocked_requires_exact_referent(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)
    args.update(verdict="BLOCKED", blocked_on="card", blocked_referent="card:parent01")
    with pytest.raises(ValueError, match="exact category referent"):
        record_review_result(tmp_path, **args)
    assert not [e for e in store._read_events("review01") if e["action"] == "review_result"]


def test_fold_rejects_combined_malformed_raw_event(tmp_path: Path) -> None:
    store, _ = _fixture(tmp_path)
    store.append_event(
        "review01",
        "review_result",
        "reviewer",
        review_card_id="review01",
        parent_card_id="wrong-parent",
        reviewer_identity="reviewer",
        claim_revision="review-r1",
        verdict="PASS",
        evidence_uri="https://example.invalid/not-local",
        evidence_sha256="0" * 64,
        blocked_on=None,
        blocked_referent=None,
        transition_id="review-result:review01:review-r1",
    )
    card = store.fold("review01")
    assert card.status.value != "done"
    assert card.owner == "reviewer"
    assert "review_result" not in card.meta
    assert card.meta["review_result_conflicts"]


@pytest.mark.parametrize(
    "replacement",
    [
        {"version": None},
        {"review_card_id": "other"},
        {"parent_card_id": "wrong-parent"},
        {"reviewer_identity": "other"},
        {"claim_revision": "stale"},
        {"verdict": "PASS_FOR_REVIEW"},
        {"evidence_uri": "https://example.invalid/evidence"},
        {"evidence_sha256": "bad"},
        {"transition_id": "wrong"},
    ],
)
def test_fold_rejects_each_malformed_binding(tmp_path: Path, replacement: dict) -> None:
    store, args = _fixture(tmp_path)
    event = dict(
        version=1,
        review_card_id="review01",
        parent_card_id="parent01",
        reviewer_identity="reviewer",
        claim_revision="review-r1",
        verdict="PASS",
        evidence_uri=args["evidence_uri"],
        evidence_sha256=args["evidence_sha256"],
        blocked_on=None,
        blocked_referent=None,
        transition_id="review-result:review01:review-r1",
    )
    event.update(replacement)
    store.append_event("review01", "review_result", "reviewer", **event)
    card = store.fold("review01")
    assert card.status.value != "done"
    assert "review_result" not in card.meta


def test_notification_failure_does_not_erase_result(tmp_path: Path) -> None:
    store, args = _fixture(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("mail unavailable")

    args.update(notify=True, notification_runner=fail)
    receipt = record_review_result(tmp_path, **args)
    assert len(receipt.notification_errors) == 2
    assert store.fold("review01").meta["review_result"]["verdict"] == "PASS"
