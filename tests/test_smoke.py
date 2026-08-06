"""Standalone smoke tests for skcoord: prove the extracted core works on its own.

The exhaustive behavioural suite still lives in skcapstone/tests (exercising these
modules through the ``skcapstone.*`` alias shims); this file guards that skcoord
stands up independently, imports cleanly with no back-reference into skcapstone at
import time, and round-trips the board + ITIL against a temp home.
"""
from __future__ import annotations

import sys

from skcoord.card import KanbanBoard, render_html
from skcoord.coordination import Board, Task
from skcoord.itil import ITILManager


def test_imports_do_not_pull_skcapstone():
    # skcoord must be import-time independent of skcapstone (one-way dependency).
    mods = [m for m in sys.modules if m == "skcapstone" or m.startswith("skcapstone.")]
    assert mods == [], f"skcoord import leaked skcapstone modules: {mods}"


def test_board_roundtrip(tmp_path):
    # Conflict-free board: Task is the spec; claim/complete are written to the
    # calling agent's own file (returns an AgentFile). Status is derived, not on Task.
    board = Board(tmp_path)
    task = Task(title="extract skcoord", priority="high")
    board.create_task(task)
    assert task.id in {t.id for t in board.load_tasks()}
    claimed = board.claim_task("lumina", task.id)
    assert claimed is not None
    completed = board.complete_task("lumina", task.id)
    assert completed is not None


def test_itil_incident_roundtrip(tmp_path):
    mgr = ITILManager(tmp_path)
    inc = mgr.create_incident(
        title="skvector down",
        source="service_health",
        affected_services=["skvector"],
        created_by="service_health",
        managed_by="lumina",
        failure_class="unreachable",
    )
    assert inc.id
    listed = mgr.list_incidents()
    assert any(i.id == inc.id for i in listed)


def test_kanban_render_html(tmp_path):
    board = Board(tmp_path)
    board.create_task(Task(title="render me", priority="medium"))
    kb = KanbanBoard(tmp_path)
    html = render_html(kb, title="SmokeBoard")
    assert "<!doctype html>" in html.lower()
