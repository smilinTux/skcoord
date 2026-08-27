"""Cross-run SUCCESS memory: meta.autopilot.successes[] writer + survival.

Spec: coord card 506782a4 (S9, "close the memory asymmetry"). Sibling of
test_failure_memory.py, same storage contract, but the load-bearing property is
different: a success must NOT be destroyed by the event that created it.
``clear_attempts`` runs on every terminal PASS (EngineeringExecutor._archive_attempts)
and wipes ``meta.autopilot.attempts[]`` wholesale, so successes cannot live there.
This file proves ``record_success`` writes to the sibling key ``successes[]`` and
that ``clear_attempts`` never touches it.
"""

from __future__ import annotations

from skcoord.coordination import Board, Task


def _card(board: Board):
    task = Task(title="success memory", priority="high")
    board.create_task(task)
    return task.id


def _successes(board: Board, task_id: str) -> list[dict]:
    task = next(t for t in board.load_tasks() if t.id == task_id)
    return ((task.meta or {}).get("autopilot", {})).get("successes", [])


def _attempts(board: Board, task_id: str) -> list[dict]:
    task = next(t for t in board.load_tasks() if t.id == task_id)
    return ((task.meta or {}).get("autopilot", {})).get("attempts", [])


def test_record_success_appends_the_distilled_entry(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_success(
        tid,
        run_id="r1",
        round=2,
        outcome="pass",
        tried="rewrote the parser",
        why_succeeded="twin gate closed: CI green, coverage 0.95",
        approach_hint="raise in the empty branch",
    )

    (entry,) = _successes(board, tid)
    assert entry["run_id"] == "r1"
    assert entry["round"] == 2
    assert entry["outcome"] == "pass"
    assert entry["tried"] == "rewrote the parser"
    assert entry["why_succeeded"] == "twin gate closed: CI green, coverage 0.95"
    assert entry["approach_hint"] == "raise in the empty branch"
    assert entry["ts"]


def test_record_success_defaults_approach_hint_to_empty(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_success(
        tid,
        run_id="r1",
        round=1,
        outcome="pass",
        tried="something",
        why_succeeded="twin gate closed",
    )

    assert _successes(board, tid)[0]["approach_hint"] == ""


def test_record_success_replaces_in_place_on_same_run_and_outcome(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_success(
        tid,
        run_id="r1",
        round=1,
        outcome="pass",
        tried="first",
        why_succeeded="first reason",
    )
    board.record_success(
        tid,
        run_id="r1",
        round=2,
        outcome="pass",
        tried="second",
        why_succeeded="second reason",
    )

    entries = _successes(board, tid)
    assert len(entries) == 1
    assert entries[0]["why_succeeded"] == "second reason"
    assert entries[0]["round"] == 2


def test_record_success_appends_on_a_distinct_key(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.record_success(
        tid, run_id="r1", round=1, outcome="pass", tried="a", why_succeeded="a"
    )
    board.record_success(
        tid, run_id="r2", round=1, outcome="pass", tried="c", why_succeeded="c"
    )  # other run, same outcome

    assert len(_successes(board, tid)) == 2


def test_record_success_caps_storage_at_ten_keeping_newest(tmp_path):
    """Corruption guard, NOT the forgetting policy (the reader bounds context)."""
    board = Board(tmp_path)
    tid = _card(board)

    for i in range(12):
        board.record_success(
            tid,
            run_id=f"r{i}",
            round=1,
            outcome="pass",
            tried=f"try {i}",
            why_succeeded=f"reason {i}",
        )

    entries = _successes(board, tid)
    assert len(entries) == 10
    assert entries[0]["why_succeeded"] == "reason 2"  # oldest two dropped
    assert entries[-1]["why_succeeded"] == "reason 11"


def test_record_success_builds_the_meta_chain_on_a_thin_card(tmp_path):
    """A card predating the field has no meta.autopilot; setdefault must build it."""
    board = Board(tmp_path)
    tid = _card(board)
    board._write_task_raw(tid, lambda d: d.pop("meta", None))

    board.record_success(
        tid,
        run_id="r1",
        round=1,
        outcome="pass",
        tried="one round",
        why_succeeded="twin gate closed",
    )

    assert len(_successes(board, tid)) == 1


def test_record_success_does_not_clobber_sibling_autopilot_keys(tmp_path):
    """successes[] is additive: scores[]/edits[]/attempts[] must survive it."""
    board = Board(tmp_path)
    tid = _card(board)
    board.score_task(tid, round=1, score=5, notes="done", harness="claude")
    board.record_attempt(
        tid, run_id="r0", round=1, outcome="ci_red", tried="a", why_failed="b"
    )

    board.record_success(
        tid, run_id="r1", round=2, outcome="pass", tried="c", why_succeeded="d"
    )

    task = next(t for t in board.load_tasks() if t.id == tid)
    ap = (task.meta or {})["autopilot"]
    assert len(ap["scores"]) == 1 and ap["scores"][0]["score"] == 5
    assert len(ap["attempts"]) == 1
    assert len(ap["successes"]) == 1


# -- the load-bearing property: clear_attempts must NEVER touch successes[] ---


def test_clear_attempts_does_not_destroy_a_recorded_success(tmp_path):
    """THE TRAP. A success recorded in the same breath as a pass must survive the
    clear_attempts call that same pass triggers (EngineeringExecutor._archive_attempts
    calls board.clear_attempts on every pass). If successes lived in attempts[]
    this would be destroyed by the very event that created it."""
    board = Board(tmp_path)
    tid = _card(board)
    board.record_attempt(
        tid,
        run_id="r0",
        round=1,
        outcome="ci_red",
        tried="a",
        why_failed="stale failure",
    )
    board.record_success(
        tid,
        run_id="r1",
        round=2,
        outcome="pass",
        tried="rewrote the parser",
        why_succeeded="twin gate closed",
    )

    removed = board.clear_attempts(tid)

    assert [e["why_failed"] for e in removed] == ["stale failure"]
    assert _attempts(board, tid) == []  # attempts cleared, as before
    assert len(_successes(board, tid)) == 1  # success SURVIVES
    assert _successes(board, tid)[0]["why_succeeded"] == "twin gate closed"


def test_clear_attempts_on_a_card_with_no_attempts_still_returns_empty_with_a_success_present(
    tmp_path,
):
    board = Board(tmp_path)
    tid = _card(board)
    board.record_success(
        tid, run_id="r1", round=1, outcome="pass", tried="a", why_succeeded="b"
    )

    assert board.clear_attempts(tid) == []
    assert len(_successes(board, tid)) == 1
