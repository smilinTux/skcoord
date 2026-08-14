"""CAB bypass guard: a raw ``status`` event may never grant approval.

``update_change(change_id, agent, new_status="approved")`` appended a status
event straight to ``approved`` with a free-text ``agent`` string and no vote,
routing around ``submit_cab_vote()`` and its no-self-approval fold guard
entirely. Every caller of that method (the MCP ``itil_change_update`` tool and
the CLI included) inherited the bypass.

``_fold_change`` already derives ``approved`` for every legitimate route via
``_cab_resolved_status`` (a qualifying CAB vote with the drafter's own vote
excluded, plus the standard / auto-normal auto-approve), so the guard only
rejects the case where no such route applied. It narrows an already-legal
transition; it never widens one.

Historical replay is exempt: ``itil_migrate_events.py`` maps legacy
``status:proposed->approved`` timeline entries onto the same ``status`` kind
but stamps ``node="migrated"``. Those approvals predate event sourcing and
their vote records cannot be re-derived, so re-gating them would demote every
migrated change back to proposed.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pytest

from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """Mirror of the fixture in ``test_change_management.py``.

    Driving a change to ``approved`` fires ``_publish_event``, which lazily
    imports ``skcapstone.pubsub`` / ``skcapstone.activity`` when that package
    is importable (this worktree lives nested inside a skcapstone checkout).
    Left in ``sys.modules`` those leak into whatever file pytest collects
    next, breaking ``test_smoke.py::test_imports_do_not_pull_skcapstone``,
    which asserts skcoord's one-way dependency on a clean slate. Scoped to
    this file; skcoord's own import-time behavior is unchanged.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


@pytest.fixture()
def mgr() -> ITILManager:
    return ITILManager(pathlib.Path(tempfile.mkdtemp()))


def test_raw_status_event_cannot_grant_approval(mgr):
    """The bypass itself: self-promote to approved with no vote."""
    chg = mgr.propose_change(title="bypass attempt", created_by="attacker")

    folded = mgr.update_change(
        chg.id, agent="attacker", new_status="approved", note="no vote cast"
    )

    assert folded.status.value == "proposed"
    conflicts = [t for t in folded.timeline if t.get("conflicted")]
    assert conflicts, "the rejected transition must be recorded, not dropped"
    assert "CAB approval required" in conflicts[0]["conflict_reason"]


def test_qualifying_cab_vote_still_approves(mgr):
    """Non-regression: the legitimate CAB route is untouched."""
    chg = mgr.propose_change(title="legit change", created_by="lumina")

    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")

    assert mgr.list_changes()[0].status.value == "approved"


def test_standard_change_auto_approve_still_works(mgr):
    """Non-regression: standard changes never needed a vote to begin with."""
    mgr.propose_change(title="std", change_type="standard", created_by="lumina")

    assert mgr.list_changes()[0].status.value == "approved"


def test_approved_to_implementing_still_works(mgr):
    """Non-regression: the guard must not strand a properly approved change."""
    chg = mgr.propose_change(title="legit", created_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")

    folded = mgr.update_change(
        chg.id, agent="lumina", new_status="implementing", note="go"
    )

    assert folded.status.value == "implementing"


def test_migrated_historical_approval_is_exempt(mgr):
    """A replayed legacy approval (node="migrated") must still fold approved."""
    chg = mgr.propose_change(title="legacy change", created_by="opus")

    # Mirror what itil_migrate_events.py writes: same `status` kind, but
    # stamped with the migration node marker rather than a real hostname.
    rid = mgr._resolve_id(mgr.changes_dir, chg.id)
    events_dir = mgr.changes_dir / rid / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / "cab-system.migrated.jsonl").write_text(
        json.dumps(
            {
                "id": "evt-legacy-1",
                "ts": "2026-07-11T11:00:00+00:00",
                "agent": "cab-system",
                "writer": "cab-system",
                "node": "migrated",
                "kind": "status",
                "to": "approved",
                "note": "Approved by: human",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert mgr.list_changes()[0].status.value == "approved"
