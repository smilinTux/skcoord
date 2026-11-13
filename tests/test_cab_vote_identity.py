"""A qualifying CAB approval needs a verified signed human grant (card bada5c86).

Before this card:

* ``submit_cab_vote(agent="human")`` with no subject folded as a qualifying
  human approval. The skcapstone MCP tool reaches exactly that call whenever
  CapAuth identity resolution fails.
* non-empty provenance fields were trusted as written, by any caller.
* a vote by the change's creator or its ``managed_by`` owner counted (only the
  ``prepared_by`` drafter was excluded).
* nothing stopped the operator seat's own identity from approving.
* the legacy clause trusted a ``decided_at`` anyone could backdate.

An unauthenticated vote may still be recorded; it never counts. Rejections are
untouched: a veto is always safe.
"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest
from cab_grants import envelope, key, pin_file, signed_vote

import skcoord.cab_authorization as cab_authorization
from skcoord.itil import (
    _LEGACY_CAB_APPROVALS,
    _PLACEHOLDER_VOTERS,
    CABDecision,
    CABDecisionValue,
    ITILManager,
    _is_human_approval,
    _qualifying_approvals,
)


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """See test_change_management.py: propose_change may import skcapstone."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _status(mgr, change_id):
    return next(c for c in mgr.list_changes() if c.id == change_id).status.value


def _change(mgr, **kw):
    kw.setdefault("title", "needs a human")
    kw.setdefault("change_type", "normal")
    kw.setdefault("created_by", "lumina")
    kw.setdefault("managed_by", "lumina")
    return mgr.propose_change(**kw)


def _vote(change_id="chg-x", **kw):
    base = dict(signed_vote(change_id), change_id=change_id)
    base.pop("subject")
    base.update(kw)
    return CABDecision(**base)


# ── authentication ──────────────────────────────────────────────────────


def test_signed_grant_from_a_pinned_approver_qualifies(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "approved"


def test_subject_less_human_vote_is_recorded_but_does_not_qualify(tmp_path):
    """The MCP fallback path: CapAuth failed, so subject=None and agent='human'."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    vote = mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert [v.agent for v in mgr.get_cab_votes(chg.id)] == ["human"]
    assert not _is_human_approval(vote)
    assert _status(mgr, chg.id) == "proposed"


def test_provenance_fields_without_an_envelope_do_not_qualify(tmp_path):
    """Any caller can write role, fingerprint and authorization id."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    forged = signed_vote(chg.id)
    forged.pop("authorization")
    mgr.submit_cab_vote(chg.id, **forged)
    assert _status(mgr, chg.id) == "proposed"


def test_self_declared_role_without_signed_authorization_does_not_qualify(tmp_path):
    """What `skoperator decide` used to write."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="atlas", managed_by="atlas")
    mgr.submit_cab_vote(
        chg.id, agent="lumina", decision="approved", subject="lumina", subject_role="approver"
    )
    assert _status(mgr, chg.id) == "proposed"


def test_unpinned_signer_does_not_qualify(tmp_path):
    """A perfectly valid signature by a key that is not pinned proves nothing."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id, subject="mallory"))
    assert _status(mgr, chg.id) == "proposed"


def test_no_pin_file_means_no_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", tmp_path / "absent.json")
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "proposed"


def test_pinned_key_must_match_the_pinned_fingerprint(tmp_path, monkeypatch):
    """A pin entry whose key is someone else's never verifies."""
    path = tmp_path / "pin.json"
    pin_file(path)
    data = json.loads(path.read_text())
    data["approvers"][0]["public_key"] = key("mallory").public_armor
    path.write_text(json.dumps(data))
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", path)
    assert not _is_human_approval(_vote())


def test_pinned_identity_must_match_the_issuer(tmp_path, monkeypatch):
    """Chef's key signing a grant that names someone else does not qualify."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id, subject="lumina-x", signer="chef"))
    assert _status(mgr, chg.id) == "proposed"


@pytest.mark.parametrize(
    "tamper",
    [
        {"change_id": "chg-other"},
        {"decision": CABDecisionValue.REJECTED},
        {"subject_role": "approver"},
        {"subject_fingerprint": "B" * 40},
        {"authorization_id": "authz-000000000000000000000000"},
        {"agent": "lumina"},
    ],
)
def test_vote_must_match_its_grant(tamper):
    assert _is_human_approval(_vote())
    assert not _is_human_approval(_vote().model_copy(update=tamper))


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "other-scope"),  # signature no longer covers the content
        ("signature", ""),
        ("schema_version", "atlas-authorization/v0"),
        ("issuer_role", "operator"),
        ("nonce", "short"),
    ],
)
def test_tampered_envelope_does_not_qualify(field, value):
    vote = _vote()
    env = dict(vote.authorization)
    env[field] = value
    assert not _is_human_approval(vote.model_copy(update={"authorization": env}))


def test_envelope_with_extra_fields_does_not_qualify():
    vote = _vote()
    env = {**vote.authorization, "extra": "x"}
    assert not _is_human_approval(vote.model_copy(update={"authorization": env}))


def test_literal_human_with_full_valid_provenance_still_does_not_qualify(tmp_path, monkeypatch):
    """`human` names no one, even with a pinned key behind it."""
    path = tmp_path / "pin.json"
    pin_file(path, identities=("human",))
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", path)
    vote = _vote(**signed_vote("chg-x", subject="human"))
    assert cab_authorization.verify_vote_authorization(vote)
    assert not _is_human_approval(vote)


@pytest.mark.parametrize("placeholder", sorted(_PLACEHOLDER_VOTERS - {""}))
def test_every_placeholder_voter_is_refused(tmp_path, monkeypatch, placeholder):
    path = tmp_path / "pin.json"
    pin_file(path, identities=(placeholder,))
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", path)
    vote = _vote(**signed_vote("chg-x", subject=placeholder))
    assert cab_authorization.verify_vote_authorization(vote)
    assert not _is_human_approval(vote)


# ── exclusions ──────────────────────────────────────────────────────────


def test_creator_vote_does_not_qualify(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="chef", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "proposed"


def test_managed_by_vote_does_not_qualify(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="lumina", managed_by="chef")
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "proposed"


def test_creator_match_ignores_spelling(tmp_path):
    """`chef`, `capauth:chef@skworld.io` and `chef@chef.skworld.io` are one person."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="capauth:chef@skworld.io", managed_by="lumina")
    mgr.submit_cab_vote(
        chg.id, **signed_vote(chg.id, subject="Chef@chef.skworld.io", signer="chef")
    )
    assert _status(mgr, chg.id) == "proposed"


def test_drafter_match_ignores_spelling(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr._append_event(
        mgr.changes_dir, chg.id, "capauth:chef@skworld.io", "pr_link", url="u", run_id="r"
    )
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "proposed"


@pytest.mark.parametrize(
    "operator",
    ["operator", "capauth:operator@skworld.io", "operator@chef.skworld.io", "atlas"],
)
def test_operator_identity_never_qualifies(tmp_path, monkeypatch, operator):
    """Even with a pinned key behind it, the operator seat is not a human."""
    path = tmp_path / "pin.json"
    pin_file(path, identities=(operator.split("@")[0].removeprefix("capauth:"),))
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", path)
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    vote = signed_vote(
        chg.id, subject=operator, signer=operator.split("@")[0].removeprefix("capauth:")
    )
    mgr.submit_cab_vote(chg.id, **vote)
    assert _status(mgr, chg.id) == "proposed"


def test_disqualified_rejection_still_blocks(tmp_path):
    """Exclusions narrow approvals only; a creator's veto still rejects."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="rejected")
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "rejected"


@pytest.mark.parametrize("placeholder", sorted(_PLACEHOLDER_VOTERS))
def test_placeholder_rejection_is_recorded_but_does_not_change_status(tmp_path, placeholder):
    """An anonymous veto is stored, yet flips nothing: a placeholder names no one."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, agent=placeholder, decision="rejected")
    assert any(v.decision.value == "rejected" for v in mgr.get_cab_votes(chg.id))
    assert _status(mgr, chg.id) == "proposed"
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "approved"


def test_placeholder_rejection_does_not_block_auto_normal(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(
        mgr, created_by="operator", tags=["auto-normal"], risk="low", rollback_plan="revert"
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="rejected")
    assert _status(mgr, chg.id) == "approved"
    mgr.submit_cab_vote(chg.id, agent="architect", decision="rejected")
    assert _status(mgr, chg.id) != "approved"


def test_creator_vote_cannot_gate_a_mid_fold_transition(tmp_path):
    """`_cab_resolved_status` applies the same exclusions as the post-loop block."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr, created_by="chef")
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    mgr.update_change(chg.id, "lumina", new_status="implementing")
    assert _status(mgr, chg.id) != "implementing"


def test_qualifying_approvals_helper_applies_every_exclusion(tmp_path, monkeypatch):
    names = ("drafter", "creator", "owner-of-record", "chef")
    path = tmp_path / "pin.json"
    pin_file(path, identities=names)
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", path)
    votes = [_vote(**signed_vote("chg-x", subject=n)) for n in names]
    votes.append(
        _vote(
            agent="human",
            subject_role="",
            subject_fingerprint="",
            authorization_id="",
            authorization=None,
        )
    )
    votes.append(_vote(**signed_vote("chg-x", subject="chef", decision="rejected")))
    got = _qualifying_approvals(
        votes, prepared_by="drafter", created_by="creator", managed_by="owner-of-record"
    )
    assert [v.agent for v in got] == ["chef"]


def test_approved_timeline_names_only_qualifying_voters(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, agent="lumina", decision="approved")
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    entry = next(
        t for t in mgr.list_changes()[0].timeline if t["action"] == "status:proposed->approved"
    )
    assert entry["note"] == "Approved by: chef"


# ── overwrite ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("decision", ["approved", "rejected", "abstain"])
def test_unauthenticated_vote_cannot_replace_a_signed_one(tmp_path, decision):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    with pytest.raises(ValueError, match="refusing to replace"):
        mgr.submit_cab_vote(chg.id, agent="chef", decision=decision)
    assert _status(mgr, chg.id) == "approved"


def test_a_signed_vote_may_replace_a_signed_one(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id, decision="rejected"))
    assert _status(mgr, chg.id) == "rejected"


# ── pinned historical approvals ─────────────────────────────────────────


def _legacy_file(mgr, change_id, agent, payload):
    mgr.ensure_dirs()
    path = mgr.cab_dir / f"{change_id}-{agent}.json"
    path.write_text(payload)
    return hashlib.sha256(payload.encode()).hexdigest()


def test_pinned_legacy_vote_keeps_its_meaning(tmp_path, monkeypatch):
    """A pinned historical vote qualifies, even by the creator (the regime it was
    written under had no such rule, and its lifecycle is terminal)."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    payload = json.dumps({"change_id": chg.id, "agent": "lumina", "decision": "approved"})
    sha = _legacy_file(mgr, chg.id, "lumina", payload)
    monkeypatch.setattr(
        "skcoord.itil._LEGACY_CAB_APPROVALS", _LEGACY_CAB_APPROVALS | {(chg.id, "lumina", sha)}
    )
    assert _status(mgr, chg.id) == "approved"
    # The drafter exclusion predates this card and still applies.
    mgr._append_event(mgr.changes_dir, chg.id, "lumina", "pr_link", url="u", run_id="r")
    assert _status(mgr, chg.id) == "proposed"


def test_backdated_unpinned_vote_does_not_qualify(tmp_path):
    """The old rule honored any vote dated before 2026-08-21."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    payload = json.dumps(
        {
            "change_id": chg.id,
            "agent": "jarvis",
            "decision": "approved",
            "decided_at": "2026-07-01T00:00:00+00:00",
        }
    )
    _legacy_file(mgr, chg.id, "jarvis", payload)
    assert _status(mgr, chg.id) == "proposed"


def test_pinned_legacy_vote_stops_qualifying_when_its_file_changes(tmp_path, monkeypatch):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    payload = json.dumps({"change_id": chg.id, "agent": "lumina", "decision": "approved"})
    sha = _legacy_file(mgr, chg.id, "lumina", payload)
    monkeypatch.setattr(
        "skcoord.itil._LEGACY_CAB_APPROVALS", _LEGACY_CAB_APPROVALS | {(chg.id, "lumina", sha)}
    )
    _legacy_file(mgr, chg.id, "lumina", payload + " ")
    assert _status(mgr, chg.id) == "proposed"


def test_legacy_vote_not_read_from_disk_never_qualifies():
    for change_id, agent, _sha in _LEGACY_CAB_APPROVALS:
        vote = CABDecision(change_id=change_id, agent=agent, decision="approved")
        assert not _is_human_approval(vote)


def test_legacy_pin_holds_exactly_the_known_votes():
    assert {(c, a) for c, a, _ in _LEGACY_CAB_APPROVALS} == {
        ("chg-11d0e1c7", "lumina"),
        ("chg-a543c87b", "lumina"),
        ("chg-a543c87b", "architect"),
        ("chg-a76c0aee", "jarvis"),
    }


def test_envelope_helper_round_trips():
    env = envelope("chg-x")
    assert cab_authorization.content_id(env) == env["authorization_id"]
