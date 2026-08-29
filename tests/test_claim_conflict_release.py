"""Exact-generation release coverage for losing concurrent claims."""

from __future__ import annotations

import json

import pytest

from skcoord.card import Column
from skcoord.card_store import CardStore, current_claim_precondition
from skcoord.coordination import AgentFile, Board, Task
from skcoord.lifecycle import audit_lifecycle

LIVE_CONFLICTS = (
    pytest.param(
        "c5134ac2",
        "codex-claim-release-rereview",
        "87f0740e79a545cbbc9006626c36242f",
        "pi-glm-chiap03-c5134ac2",
        "a180043de5264679bba0860e3ef841ac",
        "review_verdict",
        "PASS",
        id="c5134ac2",
    ),
    pytest.param(
        "600fc649",
        "codex-cardstore-recovery-review-600fc649",
        "2fb4979b184d448fb0181129b60d20e3",
        "pi-glm-chiap03-600fc649",
        "0990988d8ec244b5bb379199663fdfdc",
        "independent_review_verdict_9a7a2ec9",
        "PASS:9bb7b34841313b2dfc2c1bc1ecf8b9c6db8030eb2e0daee763c24f63d8748988",
        id="600fc649",
    ),
)


def _save_claim(board: Board, owner: str, task_id: str) -> None:
    board.save_agent(AgentFile(agent=owner, current_task=task_id, claimed_tasks=[task_id]))


def _make_conflict(
    home,
    task_id: str,
    authoritative_owner: str,
    authoritative_revision: str,
    losing_owner: str,
    losing_revision: str,
    evidence_key: str = "review_verdict",
    evidence_value: str = "PASS",
) -> tuple[Board, CardStore]:
    board = Board(home)
    board.create_task(Task(id=task_id, title=f"[REVIEW] {task_id}"))
    store = CardStore(home)
    store.append_event(
        task_id,
        "claim",
        authoritative_owner,
        owner=authoritative_owner,
        claim_revision=authoritative_revision,
    )
    _save_claim(board, authoritative_owner, task_id)
    store.append_event(
        task_id,
        "claim",
        losing_owner,
        owner=losing_owner,
        claim_revision=losing_revision,
    )
    _save_claim(board, losing_owner, task_id)
    store.append_event(
        task_id,
        "link",
        authoritative_owner,
        link_key=evidence_key,
        link_value=evidence_value,
    )
    return board, store


@pytest.mark.parametrize(
    (
        "task_id",
        "authoritative_owner",
        "authoritative_revision",
        "losing_owner",
        "losing_revision",
        "evidence_key",
        "evidence_value",
    ),
    LIVE_CONFLICTS,
)
def test_exact_losing_release_preserves_live_shape_and_allows_completion(
    tmp_path,
    task_id,
    authoritative_owner,
    authoritative_revision,
    losing_owner,
    losing_revision,
    evidence_key,
    evidence_value,
) -> None:
    board, store = _make_conflict(
        tmp_path,
        task_id,
        authoritative_owner,
        authoritative_revision,
        losing_owner,
        losing_revision,
        evidence_key,
        evidence_value,
    )
    original_logs = {
        path: path.read_bytes() for path in (tmp_path / "cards" / task_id / "events").iterdir()
    }
    original_agents = {path.name for path in board.agents_dir.iterdir()}

    conflicted = store.fold(task_id)
    assert conflicted is not None
    assert conflicted.meta["claim_conflicts"] == [
        {
            "event_id": conflicted.meta["claim_conflicts"][0]["event_id"],
            "owner": losing_owner,
            "claim_revision": losing_revision,
            "existing_owner": authoritative_owner,
            "existing_claim_revision": authoritative_revision,
            "reason": "concurrent claim requires explicit release or completion",
        }
    ]
    assert current_claim_precondition(tmp_path, task_id, losing_owner) == losing_revision
    assert audit_lifecycle(tmp_path, task_ids={task_id}).clean is False

    assert board.release_claim(
        losing_owner,
        task_id,
        actor="claim-conflict-repair",
        expected_claim_revision=losing_revision,
    )

    released = store.fold(task_id)
    assert released is not None
    assert released.owner == authoritative_owner
    assert released.status == Column.DOING
    assert released.meta["_claim_revision"] == authoritative_revision
    assert "claim_conflicts" not in released.meta
    assert released.links[evidence_key] == evidence_value
    assert {path.name for path in board.agents_dir.iterdir()} == original_agents
    losing_projection = board.load_agent(losing_owner)
    assert losing_projection is not None
    assert losing_projection.claimed_tasks == []
    assert losing_projection.current_task is None
    for path, content in original_logs.items():
        assert path.read_bytes() == content
    assert audit_lifecycle(tmp_path, task_ids={task_id}).clean is True

    board.complete_task(authoritative_owner, task_id)
    completed = store.fold(task_id)
    assert completed is not None and completed.status == Column.DONE
    assert completed.links[evidence_key] == evidence_value


def test_multiple_losing_claims_are_independently_fenced_and_retry_is_idempotent(
    tmp_path,
) -> None:
    board, store = _make_conflict(
        tmp_path,
        "multi001",
        "authoritative",
        "authoritative-revision",
        "loser-one",
        "loser-one-revision",
    )
    store.append_event(
        "multi001",
        "claim",
        "loser-two",
        owner="loser-two",
        claim_revision="loser-two-revision",
    )
    _save_claim(board, "loser-two", "multi001")

    assert board.release_claim(
        "loser-one",
        "multi001",
        actor="claim-conflict-repair",
        expected_claim_revision="loser-one-revision",
    )
    remaining = store.fold("multi001")
    assert remaining is not None
    assert [item["owner"] for item in remaining.meta["claim_conflicts"]] == ["loser-two"]
    event_count = len(store._read_events("multi001"))
    assert (
        board.release_claim(
            "loser-one",
            "multi001",
            actor="claim-conflict-repair",
            expected_claim_revision="loser-one-revision",
        )
        is False
    )
    assert len(store._read_events("multi001")) == event_count
    with pytest.raises(ValueError, match="concurrent claim conflicts"):
        board.complete_task("authoritative", "multi001")

    assert board.release_claim(
        "loser-two",
        "multi001",
        actor="claim-conflict-repair",
        expected_claim_revision="loser-two-revision",
    )
    assert audit_lifecycle(tmp_path, task_ids={"multi001"}).clean is True


@pytest.mark.parametrize("expected_revision", ["wrong-revision", "", "older-revision"])
def test_wrong_missing_or_stale_revision_appends_nothing_and_preserves_projection(
    tmp_path, expected_revision
) -> None:
    board, store = _make_conflict(
        tmp_path,
        "refuse01",
        "authoritative",
        "authoritative-revision",
        "loser",
        "current-revision",
    )
    projection = board.agent_projection_path("loser")
    projection_before = projection.read_bytes()
    events_before = store._read_events("refuse01")

    with pytest.raises(ValueError, match="claim revision conflict"):
        board.release_claim(
            "loser",
            "refuse01",
            actor="claim-conflict-repair",
            expected_claim_revision=expected_revision,
        )

    assert projection.read_bytes() == projection_before
    assert store._read_events("refuse01") == events_before


def test_missing_malformed_or_ambiguous_losing_owner_appends_nothing(tmp_path) -> None:
    board, store = _make_conflict(
        tmp_path,
        "refuse02",
        "authoritative",
        "authoritative-revision",
        "loser",
        "first-revision",
    )
    store.append_event(
        "refuse02",
        "claim",
        "loser",
        owner="loser",
        claim_revision="second-revision",
    )
    events_before = store._read_events("refuse02")
    projection = board.agent_projection_path("loser")
    projection_before = projection.read_bytes()

    with pytest.raises(ValueError, match="ambiguous"):
        board.release_claim(
            "loser",
            "refuse02",
            actor="claim-conflict-repair",
            expected_claim_revision="first-revision",
        )
    with pytest.raises(ValueError, match="canonical filename stem"):
        board.release_claim(
            "../loser",
            "refuse02",
            actor="claim-conflict-repair",
            expected_claim_revision="first-revision",
        )
    with pytest.raises(ValueError, match="claim owner missing-owner not found"):
        board.release_claim(
            "missing-owner",
            "refuse02",
            actor="claim-conflict-repair",
            expected_claim_revision="first-revision",
        )

    assert projection.read_bytes() == projection_before
    assert store._read_events("refuse02") == events_before


@pytest.mark.parametrize("bad_revision", [None, [], {}])
def test_missing_or_malformed_losing_claim_revision_is_not_releasable(
    tmp_path, bad_revision
) -> None:
    board = Board(tmp_path)
    board.create_task(Task(id="refuse03", title="[REVIEW] refuse03"))
    store = CardStore(tmp_path)
    store.append_event(
        "refuse03",
        "claim",
        "authoritative",
        owner="authoritative",
        claim_revision="authoritative-revision",
    )
    _save_claim(board, "authoritative", "refuse03")
    payload = {"owner": "loser"}
    if bad_revision is not None:
        payload["claim_revision"] = bad_revision
    store.append_event("refuse03", "claim", "loser", **payload)
    _save_claim(board, "loser", "refuse03")
    projection = board.agent_projection_path("loser")
    projection_before = projection.read_bytes()
    events_before = store._read_events("refuse03")

    with pytest.raises(ValueError, match="has no exact revision"):
        board.release_claim(
            "loser",
            "refuse03",
            actor="claim-conflict-repair",
            expected_claim_revision="guessed-revision",
        )

    assert projection.read_bytes() == projection_before
    assert store._read_events("refuse03") == events_before


def test_conflict_release_write_then_error_accepts_only_the_exact_folded_postcondition(
    tmp_path, monkeypatch
) -> None:
    board, store = _make_conflict(
        tmp_path,
        "failure1",
        "authoritative",
        "authoritative-revision",
        "loser",
        "losing-revision",
    )
    original_append = CardStore.append_event

    def append_then_raise(self, card_id, action, agent, **payload):
        event = original_append(self, card_id, action, agent, **payload)
        if action == "release_claim":
            raise OSError("event bytes were written before failure")
        return event

    monkeypatch.setattr(CardStore, "append_event", append_then_raise)
    assert board.release_claim(
        "loser",
        "failure1",
        actor="claim-conflict-repair",
        expected_claim_revision="losing-revision",
    )

    folded = store.fold("failure1")
    assert folded is not None
    assert folded.owner == "authoritative"
    assert folded.meta["_claim_revision"] == "authoritative-revision"
    assert "claim_conflicts" not in folded.meta
    assert list((tmp_path / "coordination" / "recovery").glob("*.jsonl")) == []


def test_conflict_release_postcondition_failure_restores_projection(tmp_path, monkeypatch) -> None:
    board, store = _make_conflict(
        tmp_path,
        "failure2",
        "authoritative",
        "authoritative-revision",
        "loser",
        "losing-revision",
    )
    projection = board.agent_projection_path("loser")
    projection_before = json.loads(projection.read_text(encoding="utf-8"))
    original_mirror = board._mirror_card_store

    def append_wrong_release(operation, **payload):
        if operation == "release":
            payload["expected_claim_revision"] = "wrong-revision"
        return original_mirror(operation, **payload)

    monkeypatch.setattr(board, "_mirror_card_store", append_wrong_release)
    with pytest.raises(RuntimeError, match="release transition did not reach its postcondition"):
        board.release_claim(
            "loser",
            "failure2",
            actor="claim-conflict-repair",
            expected_claim_revision="losing-revision",
        )

    projection_after = json.loads(projection.read_text(encoding="utf-8"))
    projection_after["last_seen"] = projection_before["last_seen"]
    assert projection_after == projection_before
    folded = store.fold("failure2")
    assert folded is not None and folded.meta["claim_conflicts"]
    assert folded.meta["release_conflicts"]
