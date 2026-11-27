"""A signed decision cannot be rolled back by replaying an older grant (card bada5c86).

Round 4 review: Chef approves change K with a signed grant, then rejects it with a
signed rejection. Re-submitting the old approval envelope (or writing it back under
another file name) folded K approved again. A voter's slot now takes a signed vote
only when its grant's signed ``issued_at`` is strictly later than the one held, and
the fold counts, per voter, only the signed vote with the latest ``issued_at``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from cab_grants import signed_vote

import skcoord.cab_authorization as cab_authorization
from skcoord.itil import CABDecision, ITILManager, current_votes


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _status(mgr, change_id):
    return next(c for c in mgr.list_changes() if c.id == change_id).status.value


def _change(mgr):
    return mgr.propose_change(
        title="K", change_type="normal", created_by="lumina", managed_by="lumina"
    )


def _approve_then_reject(mgr):
    chg = _change(mgr)
    old = signed_vote(chg.id)
    mgr.submit_cab_vote(chg.id, **old)
    assert _status(mgr, chg.id) == "approved"
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id, decision="rejected"))
    assert _status(mgr, chg.id) == "rejected"
    return chg, old


def test_replayed_old_approval_is_refused_and_the_change_stays_rejected(tmp_path):
    mgr = ITILManager(tmp_path)
    chg, old = _approve_then_reject(mgr)
    with pytest.raises(ValueError, match="not after the one held"):
        mgr.submit_cab_vote(chg.id, **old)
    assert _status(mgr, chg.id) == "rejected"


def test_a_grant_issued_at_the_same_time_does_not_replace(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    first = signed_vote(chg.id, decision="rejected")
    mgr.submit_cab_vote(chg.id, **first)
    with pytest.raises(ValueError, match="not after the one held"):
        mgr.submit_cab_vote(chg.id, **first)


def test_a_later_grant_replaces(tmp_path):
    mgr = ITILManager(tmp_path)
    chg, _old = _approve_then_reject(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    assert _status(mgr, chg.id) == "approved"


def test_old_approval_written_back_under_another_name_does_not_win(tmp_path):
    """A file-level replay into a second slot for the same human: ``chef `` and ``Chef``."""
    mgr = ITILManager(tmp_path)
    chg, old = _approve_then_reject(mgr)
    for name in ("chef ", "Chef"):
        vote = CABDecision(**dict(old, change_id=chg.id, agent=name))
        payload = json.dumps(vote.model_dump(), default=str)
        (mgr.cab_dir / f"{chg.id}-{name}.json").write_text(payload)
    assert len(mgr.get_cab_votes(chg.id)) == 3
    assert _status(mgr, chg.id) == "rejected"


def test_a_later_approval_in_another_slot_wins_over_an_earlier_rejection(tmp_path):
    """Latest signed issued_at per human, whichever file it sits in."""
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    rejection = CABDecision(**dict(signed_vote(chg.id, decision="rejected"), change_id=chg.id))
    approval = CABDecision(**dict(signed_vote(chg.id), change_id=chg.id, agent="Chef"))
    mgr.ensure_dirs()
    for vote in (rejection, approval):
        payload = json.dumps(vote.model_dump(), default=str)
        (mgr.cab_dir / f"{chg.id}-{vote.agent}.json").write_text(payload)
    assert _status(mgr, chg.id) == "approved"


def test_unsigned_vote_cannot_take_a_signed_voters_slot_under_another_spelling(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    mgr.submit_cab_vote(chg.id, **signed_vote(chg.id))
    for spelling in ("chef ", "CHEF", " Chef"):
        with pytest.raises(ValueError, match="refusing to replace"):
            mgr.submit_cab_vote(chg.id, agent=spelling, decision="rejected", subject=spelling)


def test_submit_strips_the_voter_name(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = _change(mgr)
    vote = mgr.submit_cab_vote(chg.id, **dict(signed_vote(chg.id), subject=" chef "))
    assert vote.agent == "chef"
    assert (mgr.cab_dir / f"{chg.id}-chef.json").exists()


def test_identity_key_strips_and_case_folds():
    key = cab_authorization.identity_key
    assert key(" Chef ") == key("chef") == key("CHEF") == key("capauth:Chef@skworld.io")
    assert key("STRASSE") == key("strasse")


def _resign(env, **changes):
    from cab_grants import PASSPHRASE, key
    from capauth.crypto import get_backend

    from skcoord.cab_authorization import content_id, signing_bytes

    env = dict(env, **changes, authorization_id="pending", signature="")
    env["authorization_id"] = content_id(env)
    env["signature"] = get_backend().sign(
        signing_bytes(env), key("chef").private_armor, PASSPHRASE
    )
    return env


def test_current_votes_breaks_a_tie_toward_rejection():
    approve = CABDecision(**dict(signed_vote("chg-t"), change_id="chg-t", agent="chef"))
    env = _resign(
        approve.authorization,
        action="itil.cab.vote.rejected",
        nonce="n" * 24,
    )
    reject = CABDecision(
        change_id="chg-t",
        agent="chef",
        decision="rejected",
        subject_role=approve.subject_role,
        subject_fingerprint=approve.subject_fingerprint,
        authorization_id=env["authorization_id"],
        authorization=env,
    )
    assert env["issued_at"] == approve.authorization["issued_at"]
    for order in ([approve, reject], [reject, approve]):
        assert [v.decision.value for v in current_votes(order)] == ["rejected"]


def test_current_votes_keeps_unsigned_votes_of_voters_without_a_signed_one():
    a = CABDecision(change_id="chg-u", agent="jarvis", decision="rejected")
    b = CABDecision(change_id="chg-u", agent="sentinel", decision="approved")
    assert current_votes([a, b]) == [a, b]


def test_rollback_message_names_the_times(tmp_path):
    mgr = ITILManager(tmp_path)
    chg, old = _approve_then_reject(mgr)
    issued = datetime.fromisoformat(old["authorization"]["issued_at"])
    assert issued < datetime.now(timezone.utc) + timedelta(seconds=1)
    with pytest.raises(ValueError, match=old["authorization"]["issued_at"][:19]):
        mgr.submit_cab_vote(chg.id, **old)
