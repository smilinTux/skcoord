"""A CAB vote counts only for the change and voter it was written for (card bada5c86).

Round 3 review: a vote file copied byte for byte under another change's name
(``A-chef.json`` to ``B-chef.json``) counted for B, because the loader trusted
the filename and the envelope was compared with the change id inside the same
copied file. The change being folded now comes from the caller, and the
filename must name the change and voter recorded inside the file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys

import pytest
from cab_grants import pin_file, signed_vote

import skcoord.cab_authorization as cab_authorization
from skcoord.cab_authorization import verify_vote_authorization
from skcoord.itil import (
    _LEGACY_CAB_APPROVALS,
    CABDecision,
    ITILManager,
    vote_file_matches,
    votes_for_change,
)


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


def _status(mgr, change_id):
    return next(c for c in mgr.list_changes() if c.id == change_id).status.value


def _two_changes(mgr):
    kw = dict(change_type="normal", created_by="lumina", managed_by="lumina")
    return mgr.propose_change(title="A", **kw), mgr.propose_change(title="B", **kw)


def _copy(mgr, src_change, dst_change, voter, dst_voter=None):
    src = mgr.cab_dir / f"{src_change}-{voter}.json"
    dst = mgr.cab_dir / f"{dst_change}-{dst_voter or voter}.json"
    shutil.copyfile(src, dst)
    return dst


def test_signed_vote_copied_under_another_change_does_not_approve_it(tmp_path):
    mgr = ITILManager(tmp_path)
    a, b = _two_changes(mgr)
    mgr.submit_cab_vote(a.id, **signed_vote(a.id))
    _copy(mgr, a.id, b.id, "chef")
    assert _status(mgr, a.id) == "approved"
    assert _status(mgr, b.id) == "proposed"
    assert mgr.get_cab_votes(b.id) == []


def test_copied_vote_with_its_change_id_edited_still_fails_the_envelope(tmp_path):
    """Rewriting the copied file's change id passes the loader but not the grant."""
    mgr = ITILManager(tmp_path)
    a, b = _two_changes(mgr)
    mgr.submit_cab_vote(a.id, **signed_vote(a.id))
    dst = _copy(mgr, a.id, b.id, "chef")
    data = json.loads(dst.read_text())
    data["change_id"] = b.id
    dst.write_text(json.dumps(data))
    assert [v.change_id for v in mgr.get_cab_votes(b.id)] == [b.id]
    assert _status(mgr, b.id) == "proposed"


def test_copied_rejection_does_not_block_another_change(tmp_path, monkeypatch):
    pin = tmp_path / "pin.json"
    pin_file(pin, identities=("chef", "casey"))
    monkeypatch.setattr(cab_authorization, "APPROVERS_FILE", pin)
    mgr = ITILManager(tmp_path / "itil")
    a, b = _two_changes(mgr)
    mgr.submit_cab_vote(a.id, **signed_vote(a.id, decision="rejected"))
    mgr.submit_cab_vote(b.id, **signed_vote(b.id, subject="casey"))
    _copy(mgr, a.id, b.id, "chef")
    assert _status(mgr, a.id) == "rejected"
    assert _status(mgr, b.id) == "approved"


def test_filename_voter_must_be_the_vote_agent(tmp_path):
    mgr = ITILManager(tmp_path)
    a, _b = _two_changes(mgr)
    mgr.submit_cab_vote(a.id, **signed_vote(a.id))
    (mgr.cab_dir / f"{a.id}-chef.json").rename(mgr.cab_dir / f"{a.id}-casey.json")
    assert mgr.get_cab_votes(a.id) == []
    assert _status(mgr, a.id) == "proposed"


def test_copied_legacy_pinned_file_does_not_approve_another_change(tmp_path, monkeypatch):
    mgr = ITILManager(tmp_path)
    a, b = _two_changes(mgr)
    mgr.ensure_dirs()
    payload = json.dumps({"change_id": a.id, "agent": "lumina", "decision": "approved"})
    (mgr.cab_dir / f"{a.id}-lumina.json").write_text(payload)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    # Pin the same file hash for both changes: the hash alone must not be enough.
    monkeypatch.setattr(
        "skcoord.itil._LEGACY_CAB_APPROVALS",
        _LEGACY_CAB_APPROVALS | {(a.id, "lumina", sha), (b.id, "lumina", sha)},
    )
    _copy(mgr, a.id, b.id, "lumina")
    assert _status(mgr, a.id) == "approved"
    assert _status(mgr, b.id) == "proposed"


def test_fold_ignores_votes_of_another_change_passed_to_it(tmp_path):
    mgr = ITILManager(tmp_path)
    a, b = _two_changes(mgr)
    foreign = CABDecision(**dict(signed_vote(a.id), change_id=a.id, agent="chef"))
    core = {"id": b.id, "change_type": "normal", "created_by": "lumina", "managed_by": "lumina"}
    folded = mgr._fold_change(core, [], [foreign])
    assert folded.status.value == "proposed"
    assert votes_for_change(b.id, [foreign]) == []
    assert votes_for_change("", [foreign]) == []


def test_verifier_binds_the_caller_change_id():
    vote = CABDecision(**dict(signed_vote("chg-a"), change_id="chg-a", agent="chef"))
    assert verify_vote_authorization(vote)
    assert verify_vote_authorization(vote, change_id="chg-a")
    assert not verify_vote_authorization(vote, change_id="chg-b")


@pytest.mark.parametrize(
    "name, ok",
    [
        ("chg-a-chef.json", True),
        ("chg-b-chef.json", False),
        ("chg-a-casey.json", False),
        ("chg-a-chef.json.bak", False),
    ],
)
def test_vote_file_matches(name, ok):
    vote = CABDecision(change_id="chg-a", agent="chef", decision="approved")
    assert vote_file_matches(name, "chg-a", vote) is ok
