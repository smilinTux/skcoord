"""A qualifying CAB approval needs an authenticated subject and role (card bada5c86).

Before this card:

* ``submit_cab_vote(agent="human")`` with no subject folded as a qualifying
  human approval. The skcapstone MCP tool reaches exactly that call whenever
  CapAuth identity resolution fails.
* a vote by the change's creator or its ``managed_by`` owner counted (only the
  ``prepared_by`` drafter was excluded).
* nothing stopped the operator seat's own identity (ATLAS runs as
  ``SKAGENT=operator``) from approving the changes it drafts.

An unauthenticated or subject-less vote may still be recorded; it just never
counts. Rejections are untouched: a veto is always safe.
"""

from __future__ import annotations

import sys

import pytest

from skcoord.itil import (
    CABDecision,
    CABDecisionValue,
    ITILManager,
    _is_human_approval,
    _qualifying_approvals,
)

FPR = "A" * 40


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """See test_change_management.py: propose_change may import skcapstone."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _authenticated(mgr, change_id, subject="chef", role="owner"):
    return mgr.submit_cab_vote(
        change_id,
        agent=subject,
        decision="approved",
        subject=subject,
        subject_role=role,
        subject_fingerprint=FPR,
        authorization_id="authz-test",
    )


def _status(mgr, change_id):
    return next(c for c in mgr.list_changes() if c.id == change_id).status.value


def _change(mgr, **kw):
    kw.setdefault("title", "needs a human")
    kw.setdefault("change_type", "normal")
    kw.setdefault("created_by", "lumina")
    kw.setdefault("managed_by", "lumina")
    return mgr.propose_change(**kw)


def test_subject_less_human_vote_is_recorded_but_does_not_qualify(tmp_path):
    """The MCP fallback path: CapAuth failed, so subject=None and agent='human'."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    vote = mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert [v.agent for v in mgr.get_cab_votes(chg.id)] == ["human"]
    assert not _is_human_approval(vote)
    assert _status(mgr, chg.id) == "proposed"


def test_literal_human_subject_without_provenance_does_not_qualify(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved", subject="human")
    assert _status(mgr, chg.id) == "proposed"


def test_self_declared_role_without_signed_authorization_does_not_qualify(tmp_path):
    """What `skoperator decide` used to write: the running agent's identity plus
    a role it asserted for itself, with no fingerprint or authorization."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="atlas", managed_by="atlas")
    mgr.submit_cab_vote(
        chg.id, agent="lumina", decision="approved", subject="lumina", subject_role="approver"
    )
    assert _status(mgr, chg.id) == "proposed"


@pytest.mark.parametrize("missing", ["subject_fingerprint", "authorization_id", "subject_role"])
def test_each_provenance_field_is_required(missing):
    fields = dict(
        change_id="chg-x",
        agent="chef",
        subject_role="owner",
        subject_fingerprint=FPR,
        authorization_id="authz-1",
        decision=CABDecisionValue.APPROVED,
        decided_at="2026-11-13T00:00:00+00:00",
    )
    assert _is_human_approval(CABDecision(**fields))
    fields[missing] = ""
    assert not _is_human_approval(CABDecision(**fields))


def test_authenticated_owner_from_a_distinct_identity_qualifies(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    _authenticated(mgr, chg.id)
    assert _status(mgr, chg.id) == "approved"


def test_creator_vote_does_not_qualify(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="chef", managed_by="lumina")
    _authenticated(mgr, chg.id, subject="chef")
    assert _status(mgr, chg.id) == "proposed"


def test_managed_by_vote_does_not_qualify(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="lumina", managed_by="chef")
    _authenticated(mgr, chg.id, subject="chef")
    assert _status(mgr, chg.id) == "proposed"


def test_creator_match_ignores_spelling(tmp_path):
    """`chef`, `capauth:chef@skworld.io` and `chef@chef.skworld.io` are one person."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="capauth:chef@skworld.io", managed_by="lumina")
    _authenticated(mgr, chg.id, subject="Chef@chef.skworld.io")
    assert _status(mgr, chg.id) == "proposed"


@pytest.mark.parametrize(
    "operator",
    [
        "operator",
        "capauth:operator@skworld.io",
        "operator@chef.skworld.io",
        "atlas",
        "device:" + "a" * 16,
    ],
)
def test_operator_identity_never_qualifies(tmp_path, operator):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    _authenticated(mgr, chg.id, subject=operator)
    assert _status(mgr, chg.id) == "proposed"


def test_disqualified_rejection_still_blocks(tmp_path):
    """Exclusions narrow approvals only; a creator's veto still rejects."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="rejected")
    _authenticated(mgr, chg.id)
    assert _status(mgr, chg.id) == "rejected"


def test_creator_vote_cannot_gate_a_mid_fold_transition(tmp_path):
    """`_cab_resolved_status` applies the same exclusions as the post-loop block."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="chef")
    _authenticated(mgr, chg.id, subject="chef")
    mgr.update_change(chg.id, "lumina", new_status="implementing")
    assert _status(mgr, chg.id) != "implementing"


def test_qualifying_approvals_helper_applies_every_exclusion():
    def vote(agent, **kw):
        base = dict(
            change_id="chg-x",
            agent=agent,
            subject_role="owner",
            subject_fingerprint=FPR,
            authorization_id="authz-1",
            decision=CABDecisionValue.APPROVED,
            decided_at="2026-11-13T00:00:00+00:00",
        )
        base.update(kw)
        return CABDecision(**base)

    votes = [
        vote("drafter"),
        vote("creator"),
        vote("owner-of-record"),
        vote("operator"),
        vote("human", subject_role="", subject_fingerprint="", authorization_id=""),
        vote("reviewer", decision=CABDecisionValue.REJECTED),
        vote("chef"),
    ]
    got = _qualifying_approvals(
        votes, prepared_by="drafter", created_by="creator", managed_by="owner-of-record"
    )
    assert [v.agent for v in got] == ["chef"]


def test_legacy_historical_approval_keeps_its_meaning(tmp_path):
    """Pre-provenance votes before the 2026-08-21 cutoff stay honored, even when
    the voter is the change's creator (the regime they were written under had
    no such rule, and their lifecycles are already terminal)."""
    legacy = CABDecision(
        change_id="chg-x",
        agent="lumina",
        decision=CABDecisionValue.APPROVED,
        decided_at="2026-07-13T04:12:55+00:00",
    )
    assert _is_human_approval(legacy)
    assert _qualifying_approvals(
        [legacy], prepared_by=None, created_by="lumina", managed_by="lumina"
    )
    # The drafter exclusion predates this card and still applies to it.
    assert not _qualifying_approvals([legacy], prepared_by="lumina")
