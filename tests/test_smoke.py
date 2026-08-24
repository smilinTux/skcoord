"""Standalone smoke tests for skcoord: prove the extracted core works on its own.

The exhaustive behavioural suite still lives in skcapstone/tests (exercising these
modules through the ``skcapstone.*`` alias shims); this file guards that skcoord
stands up independently, imports cleanly with no back-reference into skcapstone at
import time, and round-trips the board + ITIL against a temp home.
"""

from __future__ import annotations

import subprocess
import sys

from skcoord.card import KanbanBoard, render_html
from skcoord.coordination import Board, Task
from skcoord.itil import ITILManager


def test_imports_do_not_pull_skcapstone():
    # skcoord must be import-time independent of skcapstone (one-way dependency).
    # Run in a clean interpreter: in-process sys.modules is legitimately polluted
    # by any earlier test that completes a task, because completing a task takes
    # the optional lazy edge `from skcapstone.skjoule import JouleEngine`
    # (coordination.py, silently skipped when skcapstone is absent).
    code = (
        "import sys\n"
        "import skcoord.card, skcoord.coordination, skcoord.itil\n"
        "mods = [m for m in sys.modules if m == 'skcapstone' or m.startswith('skcapstone.')]\n"
        "assert mods == [], f'skcoord import leaked skcapstone modules: {mods}'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


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


def test_recurring_auto_incident_reopens_append_only(tmp_path):
    mgr = ITILManager(tmp_path)
    create = {
        "title": "service down",
        "source": "service_health",
        "affected_services": ["service"],
        "failure_class": "unreachable",
    }
    inc = mgr.create_incident(**create)
    mgr.update_incident(
        inc.id,
        "health",
        new_status="resolved",
        resolution_summary="healthy probe",
    )

    reopened = mgr.create_incident(**create)

    assert reopened.id == inc.id
    assert reopened.status.value == "investigating"
    assert reopened.resolution_summary is None
    assert any(item["action"] == "status:resolved->investigating" for item in reopened.timeline)


def test_kanban_render_html(tmp_path):
    board = Board(tmp_path)
    board.create_task(Task(title="render me", priority="medium"))
    kb = KanbanBoard(tmp_path)
    html = render_html(kb, title="SmokeBoard")
    assert "<!doctype html>" in html.lower()
