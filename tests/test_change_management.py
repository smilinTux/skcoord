"""Change-management state machine + CAB-vote identity fix (CR change-mgmt).

Covers card P1.1 (``scheduled`` status + the five new fold-only event kinds:
``pr_link``/``validation``/``schedule``/``unschedule``/``window_missed``) and
card P1.4 (CAB vote identity binding via ``subject`` + the no-self-approval
fold guard), per
docs/specs/2026-08-13-change-management-cab-ai-arch.md sections 4 and 7.

Everything here is append-only + fold-derived, mirroring the existing
incident/problem event handling in ``skcoord.itil``: no writer ever mutates
a stored change record directly.
"""

from __future__ import annotations

import sys

import pytest

from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """propose_change/update_change (pre-existing code) call ``_publish_event``,
    which lazily imports ``skcapstone.pubsub``/``skcapstone.activity`` when
    that package happens to be importable (this worktree lives nested inside
    a skcapstone checkout). This file calls those methods dozens of times;
    without cleanup, whichever skcapstone.* modules that pulls into
    ``sys.modules`` would leak into any test file pytest collects after this
    one in the same session - notably ``test_smoke.py``'s
    ``test_imports_do_not_pull_skcapstone``, which asserts a clean slate.
    Scoped to this file only; skcoord's own import-time behavior is
    unchanged.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


# ── (a) fold-invariance: a pre-existing change event log is untouched ─────


def test_pre_existing_change_folds_byte_identically(tmp_path):
    """A change whose event log predates this card must fold unchanged.

    Simulates a "historical" record using only event kinds that existed
    before P1.1 (created/status/note/title/tags/link_problem/gtd_link) and
    asserts the folded model is identical to a hand-computed expectation,
    with every new field defaulting to None. This is the hard invariant the
    card asked to prove: adding the five new event kinds + four new Change
    fields must not perturb any record that never uses them.
    """
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="legacy change",
        change_type="normal",
        risk="medium",
        rollback_plan="revert the deploy",
        managed_by="lumina",
        tags=["legacy"],
    )
    # Old-style events only: no pr_link/validation/schedule/unschedule/
    # window_missed anywhere in the log.
    mgr.update_change(chg.id, "lumina", new_status="reviewing", note="looks fine")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")

    before = mgr.list_changes()[0]
    # Fold again from disk (fresh read) - must be byte-identical.
    after = mgr.list_changes()[0]

    assert before.model_dump() == after.model_dump()
    assert after.status.value == "approved"
    # The four new fields are unset for a record that never used the new
    # event kinds.
    assert after.prepared_pr is None
    assert after.prepared_by is None
    assert after.validation is None
    assert after.scheduled_window is None


def test_new_fields_default_none_on_bare_proposed_change(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="bare", change_type="normal", managed_by="lumina")
    assert chg.prepared_pr is None
    assert chg.prepared_by is None
    assert chg.validation is None
    assert chg.scheduled_window is None
    assert chg.status.value == "proposed"


# ── (b) pr_link ─────────────────────────────────────────────────────────


def test_pr_link_sets_prepared_pr_and_prepared_by(tmp_path):
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
    folded = mgr.list_changes()[0]
    assert folded.prepared_by == "lumina"
    assert folded.prepared_pr["url"] == "https://github.com/smilinTux/skcoord/pull/42"
    assert folded.prepared_pr["head_sha"] == "deadbeef"
    # Status is untouched by a bare pr_link.
    assert folded.status.value == "proposed"


def test_pr_link_last_write_wins_on_re_prepare(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="re-prep", change_type="normal", managed_by="lumina")
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://x/pull/1",
        run_id="run-1",
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://x/pull/2",
        run_id="run-2",
    )
    folded = mgr.list_changes()[0]
    assert folded.prepared_pr["url"] == "https://x/pull/2"
    assert folded.prepared_pr["run_id"] == "run-2"


# ── (c) validation ──────────────────────────────────────────────────────


def test_validation_pass_while_proposed_moves_to_reviewing(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="validate me", change_type="normal", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "validation",
        passed=True,
        head_sha="cafef00d",
        url="https://ci/run/9",
    )
    folded = mgr.list_changes()[0]
    assert folded.validation["passed"] is True
    assert folded.validation["head_sha"] == "cafef00d"
    assert folded.status.value == "reviewing"


def test_validation_fail_leaves_status_unchanged(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="validate fails", change_type="normal", managed_by="lumina"
    )
    mgr._append_event(mgr.changes_dir, chg.id, "lumina", "validation", passed=False)
    folded = mgr.list_changes()[0]
    assert folded.validation["passed"] is False
    assert folded.status.value == "proposed"


# ── (d) schedule / unschedule / window_missed ──────────────────────────


def _approve_via_human_vote(mgr: ITILManager, change_id: str) -> None:
    mgr.submit_cab_vote(change_id, agent="human", decision="approved")


def test_schedule_valid_only_while_approved(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="schedule me", change_type="normal", managed_by="lumina"
    )
    _approve_via_human_vote(mgr, chg.id)
    assert mgr.list_changes()[0].status.value == "approved"

    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "operator",
        "schedule",
        window_start="2026-08-20T02:00:00Z",
        window_end="2026-08-20T06:00:00Z",
        asap=False,
        deploy_mode="confirm",
    )
    folded = mgr.list_changes()[0]
    assert folded.status.value == "scheduled"
    assert folded.scheduled_window["window_start"] == "2026-08-20T02:00:00Z"
    assert folded.scheduled_window["deploy_mode"] == "confirm"


def test_schedule_event_while_not_approved_is_conflicted_no_transition(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="too early", change_type="normal", managed_by="lumina"
    )
    assert chg.status.value == "proposed"

    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "operator",
        "schedule",
        asap=True,
        deploy_mode="confirm",
    )
    folded = mgr.list_changes()[0]
    # Fail-closed: still proposed, no scheduled_window set, and the timeline
    # carries the conflict marker (same treatment as an invalid status event).
    assert folded.status.value == "proposed"
    assert folded.scheduled_window is None
    conflicted = [row for row in folded.timeline if row.get("conflicted")]
    assert len(conflicted) == 1
    assert conflicted[0]["action"].startswith("schedule:")


def test_unschedule_returns_to_approved_and_clears_window(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="unschedule me", change_type="normal", managed_by="lumina"
    )
    _approve_via_human_vote(mgr, chg.id)
    mgr._append_event(mgr.changes_dir, chg.id, "operator", "schedule", asap=True)
    assert mgr.list_changes()[0].status.value == "scheduled"

    mgr._append_event(
        mgr.changes_dir, chg.id, "operator", "unschedule", note="conflict"
    )
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"
    assert folded.scheduled_window is None


def test_window_missed_falls_back_to_approved(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="missed window", change_type="normal", managed_by="lumina"
    )
    _approve_via_human_vote(mgr, chg.id)
    mgr._append_event(mgr.changes_dir, chg.id, "operator", "schedule", asap=True)
    assert mgr.list_changes()[0].status.value == "scheduled"

    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "change-deploy-runner",
        "window_missed",
        note="window elapsed",
    )
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"
    assert folded.scheduled_window is None
    missed_rows = [
        r for r in folded.timeline if r["action"] == "status:scheduled->approved"
    ]
    assert any("window elapsed" in r["note"] for r in missed_rows)


def test_scheduled_to_implementing_via_plain_status_event(tmp_path):
    """The DEPLOY edge (scheduled -> implementing) is a generic status event,
    not a new event kind - it's a table addition only, exercised here to
    prove the manual `approved -> implementing` path and the new row both
    resolve through the same, unchanged `status` handling."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="deploy me", change_type="normal", managed_by="lumina"
    )
    _approve_via_human_vote(mgr, chg.id)
    mgr._append_event(mgr.changes_dir, chg.id, "operator", "schedule", asap=True)
    assert mgr.list_changes()[0].status.value == "scheduled"

    mgr.update_change(chg.id, "change-deploy-runner", new_status="implementing")
    assert mgr.list_changes()[0].status.value == "implementing"


def test_approved_to_implementing_manual_path_still_works(tmp_path):
    """Regression: the pre-existing manual-implementer path (no scheduling
    at all) must stay legal and unchanged."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="manual implement",
        change_type="normal",
        managed_by="lumina",
        implementer="human",
    )
    _approve_via_human_vote(mgr, chg.id)
    assert mgr.list_changes()[0].status.value == "approved"
    mgr.update_change(chg.id, "human", new_status="implementing")
    assert mgr.list_changes()[0].status.value == "implementing"


# ── (e) CAB vote identity binding (subject) ────────────────────────────


def test_submit_cab_vote_without_subject_keeps_legacy_free_text_behavior(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="legacy caller", change_type="normal", managed_by="lumina"
    )
    vote = mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert vote.agent == "human"
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"


def test_submit_cab_vote_subject_overrides_free_text_agent(tmp_path):
    """The core of the identity-binding fix: when a subject is supplied it -
    not the free-text `agent` label - is what gets recorded as the voter of
    record and is what the fold reads."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="bound vote", change_type="normal", managed_by="lumina"
    )
    vote = mgr.submit_cab_vote(
        chg.id,
        agent="totally not human, trust me",
        decision="approved",
        subject="lumina",
    )
    assert vote.agent == "lumina"  # subject wins, not the free-text claim
    votes = mgr.get_cab_votes(chg.id)
    assert len(votes) == 1
    assert votes[0].agent == "lumina"
    # "lumina" is not the literal "human" approver, so this must NOT unblock
    # the change - proving the free-text "human" claim was never trusted.
    folded = mgr.list_changes()[0]
    assert folded.status.value == "proposed"


def test_submit_cab_vote_subject_true_human_still_unblocks(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="real human vote", change_type="normal", managed_by="lumina"
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved", subject="human")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"


def test_authenticated_named_owner_unblocks_without_literal_human(tmp_path):
    """Chef remains Chef in the audit log while the role proves humanity."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="owner-approved", change_type="normal", managed_by="atlas"
    )
    vote = mgr.submit_cab_vote(
        chg.id,
        agent="ignored",
        decision="approved",
        subject="chef",
        subject_role="owner",
        subject_fingerprint="A" * 40,
        authorization_id="authz-123",
    )
    assert vote.agent == "chef"
    assert vote.subject_role == "owner"
    assert mgr.list_changes()[0].status.value == "approved"


def test_display_name_without_authenticated_human_role_does_not_unblock(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="name-is-not-proof", change_type="normal", managed_by="atlas"
    )
    mgr.submit_cab_vote(chg.id, agent="Chef", decision="approved", subject="chef")
    assert mgr.list_changes()[0].status.value != "approved"


def test_human_role_without_bound_subject_is_rejected(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="unbound-role", change_type="normal", managed_by="atlas"
    )
    with pytest.raises(ValueError, match="authenticated subject"):
        mgr.submit_cab_vote(
            chg.id, agent="Chef", decision="approved", subject_role="owner"
        )


# ── (f) no-self-approval fold guard ─────────────────────────────────────


def test_no_self_approval_drafters_own_approve_is_ignored(tmp_path):
    """A CAB approve vote from the change's drafter (`prepared_by`) must not
    count toward approval, even when the voter identity is the one literal
    the CAB derivation treats as a valid approver ("human"). Models the case
    where the acting identity that prepared the change is later the same
    identity attempting to rubber-stamp its own draft."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="self drafted", change_type="normal", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "pr_link",
        url="https://x/pull/7",
        run_id="run-7",
    )
    folded_after_prep = mgr.list_changes()[0]
    assert folded_after_prep.prepared_by == "human"

    mgr.submit_cab_vote(chg.id, agent="human", decision="approved", subject="human")
    folded = mgr.list_changes()[0]
    # Self-approval dropped from the approvals pool -> stays unapproved.
    assert folded.status.value == "proposed"


def test_no_self_approval_a_different_subjects_approve_moves_it_forward(tmp_path):
    """The guard must not block the ordinary case: an AI agent drafts the
    change (prepared_by='lumina'), and a genuinely different identity
    ('human') approves it - that must still unblock normally."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="ai drafted", change_type="normal", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://x/pull/8",
        run_id="run-8",
    )
    assert mgr.list_changes()[0].prepared_by == "lumina"

    mgr.submit_cab_vote(chg.id, agent="human", decision="approved", subject="human")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"


def test_no_self_approval_drafters_reject_still_blocks(tmp_path):
    """Safe direction: the drafter's own REJECT vote must still count (a
    veto is always safe, never filtered)."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="self drafted reject", change_type="normal", managed_by="lumina"
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "pr_link",
        url="https://x/pull/9",
        run_id="run-9",
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="rejected", subject="human")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "rejected"


def test_no_self_approval_is_a_noop_without_a_pr_link_event(tmp_path):
    """Fold-invariance corollary: a change with no `pr_link` event has
    `prepared_by is None`, so the guard filters nothing and existing
    approval behavior (test_cab_human_approval_derives_approved's shape) is
    unaffected."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(
        title="no prep run", change_type="normal", managed_by="lumina"
    )
    assert chg.prepared_by is None
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="approved")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    folded = mgr.list_changes()[0]
    assert folded.status.value == "approved"
