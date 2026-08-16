"""Joule Economy work grade writer: meta.grade.

Spec: skcapstone/docs/superpowers/specs/2026-08-14-joule-economy-design.md,
section 3. Vocabulary: skcapstone/docs/superpowers/specs/
joule-grade-vocabulary.json.

skcoord is the WRITER only. This pins the storage contract: the derived
model_class rule, the disjoint size/risk vocabularies (risk must never accept
an S/M/L/XL value), idempotent re-grade, meta.autopilot surviving a grade
write, and an ungraded card still loading via load_tasks.
"""

from __future__ import annotations

import pytest

from skcoord.coordination import Board, Task


def _card(board: Board):
    task = Task(title="grade writer card", priority="medium")
    board.create_task(task)
    return task.id


def _grade(board: Board, task_id: str) -> dict:
    task = next(t for t in board.load_tasks() if t.id == task_id)
    return (task.meta or {}).get("grade", {})


# -- worked examples from joule-grade-vocabulary.json, model_class.worked_examples --

WORKED_EXAMPLES = [
    ("S", "low", "S"),
    ("S", "high", "L"),
    ("M", "crit", "XL"),
    ("L", "low", "L"),
    ("XL", "crit", "XL"),
]


@pytest.mark.parametrize("size,risk,expected_class", WORKED_EXAMPLES)
def test_model_class_matches_worked_examples(tmp_path, size, risk, expected_class):
    board = Board(tmp_path)
    tid = _card(board)

    board.set_grade(tid, size=size, risk=risk)

    assert _grade(board, tid)["model_class"] == expected_class


def test_grade_stores_all_inputs_and_a_graded_at_timestamp(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.set_grade(
        tid,
        size="M",
        risk="high",
        sensitivity="internal",
        joule_estimate=42000,
        joule_bounty=46200,
        graded_by="assessor@noroc2027",
        grader_model="ornith-1.0-35b",
        rubric_version=1,
        confidence=0.82,
        pool="private",
    )

    grade = _grade(board, tid)
    assert grade["size"] == "M"
    assert grade["risk"] == "high"
    assert grade["sensitivity"] == "internal"
    assert grade["model_class"] == "L"
    assert grade["joule_estimate"] == 42000
    assert grade["joule_bounty"] == 46200
    assert grade["graded_by"] == "assessor@noroc2027"
    assert grade["grader_model"] == "ornith-1.0-35b"
    assert grade["rubric_version"] == 1
    assert grade["confidence"] == 0.82
    assert grade["pool"] == "private"
    assert grade["graded_at"]


def test_joule_bounty_defaults_to_none_when_not_supplied(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.set_grade(tid, size="S", risk="low")

    assert _grade(board, tid)["joule_bounty"] is None


def test_risk_never_accepts_a_size_label(tmp_path):
    """risk must NEVER accept S/M/L/XL. The two axes exist to be read apart;
    this collision already happened once downstream."""
    board = Board(tmp_path)
    tid = _card(board)

    with pytest.raises(ValueError):
        board.set_grade(tid, size="M", risk="XL")


def test_set_grade_rejects_invalid_size(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    with pytest.raises(ValueError):
        board.set_grade(tid, size="huge", risk="low")


def test_set_grade_rejects_invalid_sensitivity(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    with pytest.raises(ValueError):
        board.set_grade(tid, size="M", risk="low", sensitivity="classified")


@pytest.mark.parametrize("bad_pool", ["privat", "internal", "PUBLIC", "", "public "])
def test_set_grade_rejects_invalid_pool(tmp_path, bad_pool):
    """pool gates whether a card may reach an untrusted outside worker (P5),
    so a typo like "privat" must fail loudly here rather than silently
    falling through a downstream redaction gate keyed on the wrong value."""
    board = Board(tmp_path)
    tid = _card(board)

    with pytest.raises(ValueError):
        board.set_grade(tid, size="M", risk="low", pool=bad_pool)


def test_set_grade_accepts_both_valid_pool_values(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.set_grade(tid, size="M", risk="low", pool="public")
    assert _grade(board, tid)["pool"] == "public"

    board.set_grade(tid, size="M", risk="low", pool="private")
    assert _grade(board, tid)["pool"] == "private"


def test_regrade_replaces_in_place_rather_than_appending(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.set_grade(tid, size="S", risk="low", confidence=0.5)
    board.set_grade(tid, size="XL", risk="crit", confidence=0.9)

    grade = _grade(board, tid)
    assert grade["size"] == "XL"
    assert grade["risk"] == "crit"
    assert grade["model_class"] == "XL"
    assert grade["confidence"] == 0.9
    # Only one grade block, never a list or a duplicated key.
    task = next(t for t in board.load_tasks() if t.id == tid)
    assert isinstance(task.meta["grade"], dict)


def test_grade_write_does_not_disturb_autopilot_sibling(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    board.score_task(tid, round=1, score=80, notes="baseline", harness="h1")
    board.set_grade(tid, size="M", risk="med")

    task = next(t for t in board.load_tasks() if t.id == tid)
    assert task.meta["autopilot"]["scores"][0]["notes"] == "baseline"
    assert task.meta["grade"]["size"] == "M"

    # And the reverse: grading first, then an autopilot write, must not
    # disturb meta.grade either.
    board.record_attempt(
        tid,
        run_id="r1",
        round=1,
        outcome="ci_red",
        tried="x",
        why_failed="y",
    )
    task = next(t for t in board.load_tasks() if t.id == tid)
    assert task.meta["grade"]["size"] == "M"
    assert task.meta["autopilot"]["attempts"][0]["run_id"] == "r1"


def test_ungraded_card_still_loads_via_load_tasks(tmp_path):
    board = Board(tmp_path)
    tid = _card(board)

    tasks = board.load_tasks()
    task = next(t for t in tasks if t.id == tid)
    assert "grade" not in (task.meta or {})
