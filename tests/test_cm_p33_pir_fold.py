"""PIR (post-implementation review) lifecycle guard (CR change-mgmt P3.3).

Covers the two fold-time note guards added to ``_fold_change`` per
docs/specs/2026-08-13-change-management-cab-ai-arch.md sections 3 and 9
(Phase 3b's deploy_mode=auto is explicitly out of scope for this card):

- ``deployed -> verified`` requires a non-empty PIR / smoke-check note on the
  ``verified`` status event, or it folds as a conflict entry (fail-closed).
- ``failed -> closed`` requires a non-empty rollback note on the ``closed``
  status event, same fail-closed treatment.

Neither edge is a new row in ``_CHANGE_TRANSITIONS`` - both already existed
before this card - so the guard only narrows an already-legal transition; it
never widens one. Everything here is append-only + fold-derived, mirroring
the existing P1.1 event handling in ``skcoord.itil``: no writer ever mutates
a stored change record directly.
"""

from __future__ import annotations

import sys

import pytest

from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """See test_change_management.py for why this exists: propose_change and
    update_change lazily import skcapstone.* modules via _publish_event, and
    without cleanup that leaks into sys.modules for later-collected test
    files in the same pytest session.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _deployed_change(mgr: ITILManager, title: str):
    """Walk a normal change all the way to `deployed`, human-approved."""
    chg = mgr.propose_change(
        title=title,
        change_type="normal",
        managed_by="lumina",
        rollback_plan="revert the deploy",
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert mgr.list_changes()[0].status.value == "approved"
    mgr.update_change(chg.id, "lumina", new_status="implementing")
    mgr.update_change(chg.id, "lumina", new_status="deployed", note="deploy step reported success")
    assert mgr.list_changes()[0].status.value == "deployed"
    return chg


def _failed_change(mgr: ITILManager, title: str):
    """Walk a normal change all the way to `failed`, human-approved."""
    chg = mgr.propose_change(
        title=title,
        change_type="normal",
        managed_by="lumina",
        rollback_plan="revert the deploy",
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr.update_change(chg.id, "lumina", new_status="implementing")
    mgr.update_change(chg.id, "lumina", new_status="failed", note="deploy step errored out")
    assert mgr.list_changes()[0].status.value == "failed"
    return chg


# ── (a) deployed -> verified requires a PIR note ───────────────────────────


def test_verified_without_note_folds_as_conflict_not_silent_pass(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _deployed_change(mgr, "verify me, no note")

    mgr.update_change(chg.id, "lumina", new_status="verified")  # note="" (default)
    folded = mgr.list_changes()[0]

    # Fail-closed: status stays deployed, the attempt is on the timeline as a
    # conflict entry, not a quiet no-op and not a silent pass to verified.
    assert folded.status.value == "deployed"
    conflicted = [row for row in folded.timeline if row.get("conflicted")]
    assert len(conflicted) == 1
    assert conflicted[0]["action"] == "status:deployed->verified"
    assert conflicted[0]["conflict_reason"] == "PIR note required"


def test_verified_with_note_transitions_cleanly(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _deployed_change(mgr, "verify me, with note")

    mgr.update_change(
        chg.id, "lumina", new_status="verified", note="smoke checks green, latency nominal"
    )
    folded = mgr.list_changes()[0]

    assert folded.status.value == "verified"
    assert not any(row.get("conflicted") for row in folded.timeline)
    verified_rows = [
        row for row in folded.timeline if row["action"] == "status:deployed->verified"
    ]
    assert len(verified_rows) == 1
    assert verified_rows[0]["note"] == "smoke checks green, latency nominal"


def test_verified_blank_whitespace_note_still_conflicts(tmp_path):
    """A note that is present but blank (whitespace-only) must not satisfy
    the guard - the check is `.strip()`, not merely "the key exists"."""
    mgr = ITILManager(tmp_path)
    chg = _deployed_change(mgr, "verify me, whitespace note")

    mgr.update_change(chg.id, "lumina", new_status="verified", note="   ")
    folded = mgr.list_changes()[0]

    assert folded.status.value == "deployed"
    conflicted = [row for row in folded.timeline if row.get("conflicted")]
    assert len(conflicted) == 1
    assert conflicted[0]["conflict_reason"] == "PIR note required"


# ── (b) failed -> closed requires a rollback note ──────────────────────────


def test_closed_without_rollback_note_folds_as_conflict(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _failed_change(mgr, "rollback me, no note")

    mgr.update_change(chg.id, "lumina", new_status="closed")  # note="" (default)
    folded = mgr.list_changes()[0]

    assert folded.status.value == "failed"
    conflicted = [row for row in folded.timeline if row.get("conflicted")]
    assert len(conflicted) == 1
    assert conflicted[0]["action"] == "status:failed->closed"
    assert conflicted[0]["conflict_reason"] == "rollback note required"


def test_closed_with_rollback_note_transitions_cleanly(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _failed_change(mgr, "rollback me, with note")

    mgr.update_change(
        chg.id, "lumina", new_status="closed", note="rolled back via revert commit abc123"
    )
    folded = mgr.list_changes()[0]

    assert folded.status.value == "closed"
    assert not any(row.get("conflicted") for row in folded.timeline)


def test_failed_to_implementing_retry_does_not_require_a_note(tmp_path):
    """Regression: the retry edge (failed -> implementing) is NOT gated -
    only failed -> closed is. A bare retry must keep working unchanged."""
    mgr = ITILManager(tmp_path)
    chg = _failed_change(mgr, "retry me")

    mgr.update_change(chg.id, "lumina", new_status="implementing")
    folded = mgr.list_changes()[0]

    assert folded.status.value == "implementing"
    assert not any(row.get("conflicted") for row in folded.timeline)


def test_rejected_to_closed_does_not_require_a_note(tmp_path):
    """Regression: `rejected -> closed` is a different edge than `failed ->
    closed` and is NOT gated by this card - only the rollback-specific edge
    is."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="reject me", change_type="normal", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="rejected")
    assert mgr.list_changes()[0].status.value == "rejected"

    mgr.update_change(chg.id, "human", new_status="closed")
    folded = mgr.list_changes()[0]

    assert folded.status.value == "closed"
    assert not any(row.get("conflicted") for row in folded.timeline)


# ── (c) fold-invariance: every existing change record folds unchanged ─────


def test_pre_existing_change_folds_byte_identically(tmp_path):
    """A change whose event log never touches the two new-gated edges
    (deployed->verified, failed->closed) must fold exactly as it did before
    this card - mirrors test_change_management.py's
    ``test_pre_existing_change_folds_byte_identically`` (P1.1) for the P3.3
    guard: adding a narrower acceptance rule to two specific transitions must
    not perturb any record that never reaches them.
    """
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="legacy change, never verified or rollback-closed",
        change_type="normal",
        risk="medium",
        rollback_plan="revert the deploy",
        managed_by="lumina",
        tags=["legacy"],
    )
    mgr.update_change(chg.id, "lumina", new_status="reviewing", note="looks fine")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")

    before = mgr.list_changes()[0]
    # Fold again from disk (fresh read) - must be byte-identical.
    after = mgr.list_changes()[0]

    assert before.model_dump() == after.model_dump()
    assert after.status.value == "approved"
    # NOTE: mirrors test_change_management.py's P1.1 fold-invariance test
    # exactly, including its pre-existing quirk that the "reviewing" status
    # event (appended before the CAB vote in wall-clock order) folds
    # conflicted because _cab_resolved_status derives "approved" from the
    # already-on-disk vote regardless of event order - unrelated to the PIR
    # guard added by this card. The point being proven here is narrower and
    # unaffected by that quirk: this record's fold is byte-identical across
    # repeated reads, and it never engages the new deployed->verified /
    # failed->closed note guards at all.


def test_deployed_change_with_no_verified_attempt_folds_byte_identically(tmp_path):
    """A change that reaches `deployed` and stops there (the common case
    before an operator gets to the PIR) must fold identically across repeated
    reads - the guard only activates on an actual `verified`-targeted event,
    never merely by being in the `deployed` state."""
    mgr = ITILManager(tmp_path)
    _deployed_change(mgr, "deployed, not yet verified")

    before = mgr.list_changes()[0]
    after = mgr.list_changes()[0]

    assert before.model_dump() == after.model_dump()
    assert after.status.value == "deployed"


def test_deployed_to_verified_with_note_is_fold_pure(tmp_path):
    """A legitimate, already-noted PIR event (this was always a legal
    ``status`` event shape before this card; only the empty-note case is
    newly refused) folds identically on repeated reads - the pure-fold
    contract this whole store relies on."""
    mgr = ITILManager(tmp_path)
    chg = _deployed_change(mgr, "pir fold purity")
    mgr.update_change(chg.id, "lumina", new_status="verified", note="smoke checks green")

    before = mgr.list_changes()[0]
    after = mgr.list_changes()[0]

    assert before.model_dump() == after.model_dump()
    assert after.status.value == "verified"
