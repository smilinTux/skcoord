"""Cross-run failure memory: meta.autopilot.attempts[] writer + clear.

Spec: skharness/docs/specs/2026-08-14-skharness-failure-memory.md (FM-1, FM-2).

skcoord stores the facts; skharness decides what an agent reads. These tests pin
the storage contract only: the append/replace shape, the corruption cap, and the
fact that clear_attempts hands the removed entries BACK (skcoord has no journal
code -- skharness archives them).
"""

from __future__ import annotations

from skcoord.coordination import Board, Task


def _card(board: Board):
    task = Task(title="failure memory", priority="high")
    board.create_task(task)
    return task.id


def _attempts(board: Board, task_id: str) -> list[dict]:
    task = next(t for t in board.load_tasks() if t.id == task_id)
    return ((task.meta or {}).get("autopilot", {})).get("attempts", [])


def test_record_attempt_appends_the_distilled_entry(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_attempt(
        tid,
        run_id="r1",
        round=3,
        outcome="ci_red",
        tried="rewrote the parser",
        why_failed="test_parse_empty asserts ValueError, got None",
        replacement_hint="raise in the empty branch",
    )

    (entry,) = _attempts(board, tid)
    assert entry["run_id"] == "r1"
    assert entry["round"] == 3
    assert entry["outcome"] == "ci_red"
    assert entry["tried"] == "rewrote the parser"
    assert entry["why_failed"] == "test_parse_empty asserts ValueError, got None"
    assert entry["replacement_hint"] == "raise in the empty branch"
    assert entry["ts"]


def test_record_attempt_defaults_replacement_hint_to_empty(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_attempt(
        tid,
        run_id="r1",
        round=1,
        outcome="no_op",
        tried="nothing",
        why_failed="no diff in 2 rounds",
    )

    assert _attempts(board, tid)[0]["replacement_hint"] == ""


def test_record_attempt_replaces_in_place_on_same_run_and_outcome(tmp_path):
    """Idempotency key is (run_id, outcome): a retried finalize or a crash-resume
    must not double-record the same failure."""
    board = Board(tmp_path)
    tid = _card(board)

    board.record_attempt(
        tid,
        run_id="r1",
        round=1,
        outcome="ci_red",
        tried="first",
        why_failed="first cause",
    )
    board.record_attempt(
        tid,
        run_id="r1",
        round=2,
        outcome="ci_red",
        tried="second",
        why_failed="second cause",
    )

    entries = _attempts(board, tid)
    assert len(entries) == 1
    assert entries[0]["why_failed"] == "second cause"
    assert entries[0]["round"] == 2


def test_record_attempt_appends_on_a_distinct_key(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_attempt(
        tid, run_id="r1", round=1, outcome="ci_red", tried="a", why_failed="a"
    )
    board.record_attempt(
        tid, run_id="r1", round=1, outcome="no_op", tried="b", why_failed="b"
    )  # same run, other outcome
    board.record_attempt(
        tid, run_id="r2", round=1, outcome="ci_red", tried="c", why_failed="c"
    )  # other run, same outcome

    assert len(_attempts(board, tid)) == 3


def test_record_attempt_caps_storage_at_ten_keeping_newest(tmp_path):
    """Corruption guard, NOT the forgetting policy (the reader bounds context)."""
    board = Board(tmp_path)
    tid = _card(board)

    for i in range(12):
        board.record_attempt(
            tid,
            run_id=f"r{i}",
            round=1,
            outcome="ci_red",
            tried=f"try {i}",
            why_failed=f"cause {i}",
        )

    entries = _attempts(board, tid)
    assert len(entries) == 10
    assert entries[0]["why_failed"] == "cause 2"  # oldest two dropped
    assert entries[-1]["why_failed"] == "cause 11"


def test_record_attempt_builds_the_meta_chain_on_a_thin_card(tmp_path):
    """A card predating the field has no meta.autopilot; setdefault must build it."""
    board = Board(tmp_path)
    tid = _card(board)
    board._write_task_raw(tid, lambda d: d.pop("meta", None))

    board.record_attempt(
        tid,
        run_id="r1",
        round=1,
        outcome="direct_fail",
        tried="one ungated round",
        why_failed="empty diff",
    )

    assert len(_attempts(board, tid)) == 1


def test_record_attempt_does_not_clobber_sibling_autopilot_keys(tmp_path):
    """attempts[] is additive: scores[]/edits[] and friends must survive it."""
    board = Board(tmp_path)
    tid = _card(board)
    board.score_task(tid, round=1, score=3, notes="not yet", harness="claude")

    board.record_attempt(
        tid, run_id="r1", round=1, outcome="ci_red", tried="a", why_failed="b"
    )

    task = next(t for t in board.load_tasks() if t.id == tid)
    ap = (task.meta or {})["autopilot"]
    assert len(ap["scores"]) == 1 and ap["scores"][0]["score"] == 3
    assert len(ap["attempts"]) == 1


def test_clear_attempts_wipes_the_card_and_returns_the_removed_entries(tmp_path):
    """skcoord clears; skharness archives what comes back. No journal write here."""
    board = Board(tmp_path)
    tid = _card(board)
    board.record_attempt(
        tid, run_id="r1", round=1, outcome="ci_red", tried="a", why_failed="cause a"
    )
    board.record_attempt(
        tid, run_id="r2", round=2, outcome="no_op", tried="b", why_failed="cause b"
    )

    removed = board.clear_attempts(tid)

    assert [e["why_failed"] for e in removed] == ["cause a", "cause b"]
    assert _attempts(board, tid) == []


def test_clear_attempts_on_a_card_with_no_attempts_returns_empty(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    assert board.clear_attempts(tid) == []
    assert _attempts(board, tid) == []
