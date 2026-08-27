"""Change-mgmt P2.4: kanban column mapping + card meta passthrough.

Covers card P2.4 (721fded0), docs/specs/2026-08-13-change-management-cab-ai-arch.md
section 8: ``_CHANGE_COLUMN`` gains ``"scheduled": Column.READY`` and the
change lane reads backlog=proposed; ready=reviewing/approved/scheduled;
doing=implementing/failed; review=deployed; done=verified/closed/rejected.
Also covers ``card_from_change`` passing the P1.1 fold fields (prepared_pr,
prepared_by, validation, scheduled_window) through into ``Card.meta`` so a
dashboard client renders a change card without a second fetch of the raw
ITIL record, and the precise (event-kind-based, not note-text-guessed)
``window_missed`` flag.
"""

from __future__ import annotations

import sys

import pytest

from skcoord.card import (
    _CHANGE_COLUMN,
    Column,
    KanbanBoard,
    _change_window_missed,
    card_from_change,
)
from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """See test_change_management.py: propose_change/update_change lazily pull
    in skcapstone.* modules via ``_publish_event``; scrub them so this file
    never leaks state into test_smoke.py's clean-import assertion.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


# --------------------------------------------------------------------------- #
# _CHANGE_COLUMN: the exact lane mapping
# --------------------------------------------------------------------------- #


def test_change_column_gains_scheduled_as_ready():
    assert _CHANGE_COLUMN["scheduled"] == Column.READY


def test_change_column_full_lane_mapping():
    assert _CHANGE_COLUMN == {
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


def test_change_column_ready_bucket_matches_design_doc():
    ready = {status for status, col in _CHANGE_COLUMN.items() if col == Column.READY}
    assert ready == {"reviewing", "approved", "scheduled"}


def test_change_column_done_bucket_matches_design_doc():
    done = {status for status, col in _CHANGE_COLUMN.items() if col == Column.DONE}
    assert done == {"verified", "closed", "rejected"}


def test_change_column_doing_bucket_matches_design_doc():
    doing = {status for status, col in _CHANGE_COLUMN.items() if col == Column.DOING}
    assert doing == {"implementing", "failed"}


# --------------------------------------------------------------------------- #
# card_from_change: meta passthrough
# --------------------------------------------------------------------------- #


def test_card_from_change_passes_through_none_fields_on_a_bare_change(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="bare", change_type="normal", managed_by="lumina")
    folded = mgr.list_changes()[0]

    card = card_from_change(folded, events=[])

    assert card.meta["itil_status"] == "proposed"
    assert card.meta["prepared_pr"] is None
    assert card.meta["prepared_by"] is None
    assert card.meta["validation"] is None
    assert card.meta["scheduled_window"] is None
    assert card.meta["window_missed"] is False
    assert card.status == Column.BACKLOG
    assert card.id == chg.id


def test_card_from_change_passes_through_prepared_pr_and_validation(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="prep me", change_type="normal", managed_by="lumina")
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://github.com/smilinTux/skcoord/pull/42",
        branch="chg/prep-me",
        run_id="run-abc",
        head_sha="deadbeef",
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "ci",
        "validation",
        passed=True,
        head_sha="deadbeef",
        url="https://ci/run/1",
        summary="3/3 checks passed",
        checks=[{"name": "pytest", "bucket": "pass"}],
    )
    folded = mgr.list_changes()[0]
    events = mgr._read_events(mgr.changes_dir, chg.id)

    card = card_from_change(folded, events)

    assert card.meta["itil_status"] == "reviewing"  # validation pass auto-advances
    assert card.meta["prepared_by"] == "lumina"
    assert (
        card.meta["prepared_pr"]["url"]
        == "https://github.com/smilinTux/skcoord/pull/42"
    )
    assert card.meta["validation"]["passed"] is True
    assert card.meta["validation"]["head_sha"] == "deadbeef"
    assert card.status == Column.READY


def test_card_from_change_scheduled_lands_in_ready_column(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="scheduled thing", change_type="standard", managed_by="lumina"
    )
    # standard auto-approves at fold time
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-20T02:00:00+00:00",
        window_end="2026-08-20T06:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )
    folded = mgr.list_changes()[0]
    events = mgr._read_events(mgr.changes_dir, chg.id)

    card = card_from_change(folded, events)

    assert folded.status.value == "scheduled"
    assert card.status == Column.READY
    assert card.meta["scheduled_window"]["window_start"] == "2026-08-20T02:00:00+00:00"
    assert card.meta["window_missed"] is False


def test_card_from_change_without_events_defaults_window_missed_false(tmp_path):
    """The events=None default path (a caller that has not fetched the raw
    log) must never guess "missed" - it conservatively reports False."""
    mgr = ITILManager(tmp_path)
    mgr.propose_change(
        title="no events passed", change_type="normal", managed_by="lumina"
    )
    folded = mgr.list_changes()[0]

    card = card_from_change(folded)

    assert card.meta["window_missed"] is False


# --------------------------------------------------------------------------- #
# _change_window_missed: precise, event-kind-based (not note-text-guessed)
# --------------------------------------------------------------------------- #


def test_window_missed_true_when_last_lifecycle_event_is_window_missed(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="misses its window", change_type="standard", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-01T04:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )
    # Custom note text (not the fold's default "window missed" string) -
    # proves this is event-kind based, not a note-text guess.
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "change-deploy-runner",
        "window_missed",
        note="window elapsed at 2026-08-01T05:00:00Z",
    )
    events = mgr._read_events(mgr.changes_dir, chg.id)

    assert _change_window_missed(events) is True


def test_window_missed_false_when_last_lifecycle_event_is_unschedule(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="operator unscheduled", change_type="standard", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-01T04:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )
    mgr._append_event(mgr.changes_dir, chg.id, "human", "unschedule", note="")
    events = mgr._read_events(mgr.changes_dir, chg.id)

    assert _change_window_missed(events) is False


def test_window_missed_false_when_re_scheduled_after_a_miss(tmp_path):
    """A re-schedule after a missed window must clear the miss flag: the
    last lifecycle event is the new `schedule`, not the earlier miss."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="re-scheduled", change_type="standard", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-01T04:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )
    mgr._append_event(
        mgr.changes_dir, chg.id, "change-deploy-runner", "window_missed", note=""
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-02T00:00:00+00:00",
        window_end="2026-08-02T04:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )
    events = mgr._read_events(mgr.changes_dir, chg.id)

    assert _change_window_missed(events) is False


def test_window_missed_false_with_no_lifecycle_events():
    assert _change_window_missed([]) is False


# --------------------------------------------------------------------------- #
# KanbanBoard.cards(): end-to-end placement in the grid
# --------------------------------------------------------------------------- #


def test_kanban_board_places_scheduled_change_in_ready(tmp_path, monkeypatch):
    # Force the legacy coord+ITIL projection: the event-sourced CardStore
    # (Phase 4, default-ON per card_store.card_store_read_enabled) is a
    # separate write path with no independent ITIL projection of its own,
    # and this test seeds data through ITILManager directly.
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="board placement", change_type="standard", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "schedule",
        window_start="2026-08-20T02:00:00+00:00",
        window_end="2026-08-20T06:00:00+00:00",
        asap=False,
        deploy_mode="confirm",
    )

    kb = KanbanBoard(tmp_path)
    cards = {c.id: c for c in kb.cards()}

    assert chg.id in cards
    assert cards[chg.id].status == Column.READY
    assert cards[chg.id].meta["scheduled_window"] is not None

    grid = kb.grid()
    ready_ids = {c.id for c in grid["change"]["ready"]}
    assert chg.id in ready_ids
