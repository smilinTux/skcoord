"""CAB provenance-upgrade compatibility for already-terminal changes."""

from __future__ import annotations

import sys

import pytest

from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """Keep optional publish adapters out of later import-boundary tests."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _legacy_terminal_fixture(tmp_path):
    mgr = ITILManager(tmp_path)
    change_id = "chg-a76c0aee"
    mgr._write_core(
        mgr.changes_dir,
        change_id,
        {
            "id": change_id,
            "type": "change",
            "title": "legacy estate rollout",
            "change_type": "normal",
            "risk": "high",
            "rollback_plan": "restore the prior package set",
            "test_plan": "run identity, memory, and service acceptance",
            "managed_by": "jarvis",
            "created_by": "jarvis",
            "cab_required": True,
            "created_at": "2026-08-20T22:05:42+00:00",
            "tags": ["estate"],
        },
    )
    mgr._append_event(mgr.changes_dir, change_id, "jarvis", "created", note="RFC")
    mgr.submit_cab_vote(
        change_id,
        agent="jarvis",
        decision="approved",
        conditions="Human owner authorized the bounded rollout in the recorded session.",
    )
    mgr.update_change(change_id, "jarvis", new_status="reviewing", note="reviewed")
    mgr.update_change(change_id, "jarvis", new_status="approved", note="legacy raw approval")
    mgr.update_change(change_id, "jarvis", new_status="implementing", note="started")
    mgr.update_change(change_id, "jarvis", new_status="deployed", note="deployed")
    mgr.update_change(
        change_id,
        "jarvis",
        new_status="verified",
        note="PIR and smoke checks passed",
    )
    mgr.update_change(change_id, "jarvis", new_status="closed", note="closed successful")
    return mgr, change_id


def test_hash_bound_migration_preserves_historical_closed_fold(tmp_path):
    mgr, change_id = _legacy_terminal_fixture(tmp_path)
    assert mgr.list_changes()[0].status.value == "reviewing"

    before = mgr._read_events(mgr.changes_dir, change_id)
    plan = mgr.migrate_legacy_terminal_change(change_id)
    assert plan["schema"] == "skcoord.itil.legacy-terminal/v1"
    assert len(plan["evidence_sha256"]) == 64
    assert plan["applied"] is False
    assert mgr._read_events(mgr.changes_dir, change_id) == before

    applied = mgr.migrate_legacy_terminal_change(change_id, apply=True)
    assert applied["applied"] is True
    folded = mgr.list_changes()[0]
    assert folded.status.value == "closed"
    assert any(row["action"] == "legacy-terminal-migration:closed" for row in folded.timeline)

    event_count = len(mgr._read_events(mgr.changes_dir, change_id))
    repeated = mgr.migrate_legacy_terminal_change(change_id, apply=True)
    assert repeated["idempotent"] is True
    assert len(mgr._read_events(mgr.changes_dir, change_id)) == event_count


def test_new_creator_cannot_self_approve_with_legacy_vote_or_raw_status(tmp_path):
    mgr = ITILManager(tmp_path)
    change = mgr.propose_change(title="self approval", created_by="human", managed_by="human")

    mgr.submit_cab_vote(change.id, agent="human", decision="approved")
    mgr.update_change(change.id, "human", new_status="approved", note="self asserted")

    folded = mgr.list_changes()[0]
    assert folded.status.value == "proposed"
    assert any(
        "CAB approval required" in row.get("conflict_reason", "") for row in folded.timeline
    )
    with pytest.raises(ValueError, match="pre-provenance"):
        mgr.migrate_legacy_terminal_change(change.id, apply=True)


def test_migration_refuses_incomplete_or_unverified_history(tmp_path):
    mgr = ITILManager(tmp_path)
    change = mgr.propose_change(title="not terminal", created_by="jarvis")
    mgr.submit_cab_vote(change.id, agent="jarvis", decision="approved")
    mgr.update_change(change.id, "jarvis", new_status="implementing")
    mgr.update_change(change.id, "jarvis", new_status="deployed")
    mgr.update_change(change.id, "jarvis", new_status="verified", note="")
    mgr.update_change(change.id, "jarvis", new_status="closed")

    with pytest.raises(ValueError, match=r"verified\(PIR\)"):
        mgr.migrate_legacy_terminal_change(change.id, apply=True)


def test_forged_or_unbound_marker_is_ignored(tmp_path):
    mgr, change_id = _legacy_terminal_fixture(tmp_path)
    mgr._append_event(
        mgr.changes_dir,
        change_id,
        "attacker",
        "legacy_terminal_migration",
        node="migrated",
        schema="skcoord.itil.legacy-terminal/v1",
        to="closed",
        evidence_sha256="0" * 64,
    )

    assert mgr.list_changes()[0].status.value == "reviewing"
