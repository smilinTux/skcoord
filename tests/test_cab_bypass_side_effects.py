"""CAB bypass guard: the side effects must respect the fold, not the request.

``_fold_change``'s CAB bypass guard (see ``test_cab_bypass_fold.py``) already
refuses a raw ``status`` event that tries to grant approval, so the record's
own status stays ``proposed``. But ``update_change`` fired the approval side
effects - the ``itil.change.approved`` pubsub event and the high-priority
``[ITIL:<id>] Implement: <title>`` GTD next-action - keyed only on the
REQUESTED status, before the fold ever ran. A blocked self-approval therefore
still announced an approval to every bus consumer and landed an implement task
on the operator's board: the system's internal state was right while
everything it told the outside world was wrong.

The fix fires those side effects from the FOLD RESULT. These tests pin both
directions - refused approval emits nothing, legitimate approval still emits -
so a future refactor cannot silently reconnect the emission to the request.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

import pytest

from skcoord.itil import ITILManager


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """Mirror of the fixture in ``test_cab_bypass_fold.py``.

    ``_publish_event`` lazily imports ``skcapstone.pubsub`` /
    ``skcapstone.activity`` when that package is importable (this worktree
    lives nested inside a skcapstone checkout). Left in ``sys.modules`` those
    leak into whatever file pytest collects next, breaking
    ``test_smoke.py::test_imports_do_not_pull_skcapstone``.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


@pytest.fixture()
def mgr(monkeypatch) -> ITILManager:
    """A manager whose two outward-facing side effects are captured, not fired."""
    m = ITILManager(pathlib.Path(tempfile.mkdtemp()))
    m.published: list[str] = []
    m.gtd_texts: list[str] = []

    def _capture_publish(topic, payload):
        m.published.append(topic)

    def _capture_gtd(text, source_ref, status, priority=None):
        m.gtd_texts.append(text)
        return f"gtd-{len(m.gtd_texts)}"

    monkeypatch.setattr(m, "_publish_event", _capture_publish)
    monkeypatch.setattr(m, "_gtd_emit", _capture_gtd)
    return m


def test_refused_approval_publishes_nothing_and_emits_no_gtd_task(mgr):
    """The measured bug: guard HELD, side effects fired anyway."""
    chg = mgr.propose_change(title="bypass attempt", created_by="attacker", implementer="attacker")
    mgr.published.clear()  # drop itil.change.proposed from the create call

    folded = mgr.update_change(
        chg.id, agent="attacker", new_status="approved", note="no vote cast"
    )

    assert folded.status.value == "proposed", "precondition: the fold guard holds"
    assert "itil.change.approved" not in mgr.published
    assert mgr.gtd_texts == []
    assert folded.gtd_item_ids == []


def test_legitimate_cab_approval_still_publishes_and_emits(mgr):
    """Positive control: the fix must not be 'switch the side effects off'."""
    chg = mgr.propose_change(title="legit", created_by="lumina", implementer="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr.published.clear()

    folded = mgr.update_change(chg.id, agent="human", new_status="approved", note="CAB ok")

    assert folded.status.value == "approved"
    assert "itil.change.approved" in mgr.published
    assert mgr.gtd_texts == [f"[ITIL:{folded.id}] Implement: legit"]
    assert folded.gtd_item_ids == ["gtd-1"]


def test_refused_deploy_publishes_no_deployed_event(mgr):
    """Same emission site, same shape: a refused deploy must stay quiet too."""
    chg = mgr.propose_change(title="not implementing yet", created_by="lumina")
    mgr.published.clear()

    folded = mgr.update_change(chg.id, agent="lumina", new_status="deployed", note="jump")

    assert folded.status.value == "proposed"
    assert "itil.change.deployed" not in mgr.published


def test_legitimate_deploy_still_publishes(mgr):
    """Positive control for the deployed edge."""
    chg = mgr.propose_change(title="std deploy", change_type="standard", created_by="lumina")
    mgr.update_change(chg.id, agent="lumina", new_status="implementing")
    mgr.published.clear()

    folded = mgr.update_change(chg.id, agent="lumina", new_status="deployed", note="shipped")

    assert folded.status.value == "deployed"
    assert "itil.change.deployed" in mgr.published


def test_approval_refused_by_an_invalid_transition_stays_quiet_too(mgr):
    """The other way the fold refuses: not the CAB guard, an illegal edge.

    ``_fold_change`` also declines a ``status`` event whose edge is not in
    ``_CHANGE_TRANSITIONS`` (recorded conflicted, no transition). That branch
    reaches the same emission site, so it needs the same pin: a change already
    ``deployed`` cannot be walked back to ``approved``, and nothing about that
    refused request may reach the bus or the operator's board.
    """
    chg = mgr.propose_change(
        title="already shipped", change_type="standard", created_by="lumina", implementer="lumina"
    )
    mgr.update_change(chg.id, agent="lumina", new_status="implementing")
    mgr.update_change(chg.id, agent="lumina", new_status="deployed", note="shipped")
    mgr.published.clear()
    mgr.gtd_texts.clear()

    folded = mgr.update_change(chg.id, agent="lumina", new_status="approved", note="re-approve?")

    assert folded.status.value == "deployed", "precondition: the illegal edge did not take"
    assert "itil.change.approved" not in mgr.published
    assert mgr.gtd_texts == []


def test_reapproving_an_already_approved_change_emits_no_second_task(mgr):
    """A no-op re-approve must not land a second implement task.

    One step further into the same family: a change approved by CAB vote
    already folds ``approved`` before ``update_change`` runs, so re-issuing
    ``new_status="approved"`` (the CLI run twice, a retried MCP call) passes
    the fold check while the fold moves nothing. That put a SECOND
    high-priority ``[ITIL:<id>] Implement: <title>`` on the operator's board
    for one approval.
    """
    chg = mgr.propose_change(title="legit", created_by="lumina", implementer="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr.update_change(chg.id, agent="human", new_status="approved", note="CAB ok")

    assert mgr.gtd_texts == [f"[ITIL:{chg.id}] Implement: legit"], "precondition: the real one"

    folded = mgr.update_change(chg.id, agent="human", new_status="approved", note="again")

    assert folded.status.value == "approved"
    assert mgr.gtd_texts == [f"[ITIL:{chg.id}] Implement: legit"], "no duplicate task"
    assert folded.gtd_item_ids == ["gtd-1"]


def test_refused_approval_does_not_block_a_later_real_approval(mgr):
    """The dedup guard must not be poisonable by a bypass attempt.

    ``gtd_item_ids`` is read from ``gtd_link`` events, which are only appended
    after an emission that actually happened - so a refused approval leaves it
    empty and the genuine approval that follows still emits. A guard keyed on
    the raw event log instead would let an attacker permanently silence a
    change's approval by firing one refused approve first.
    """
    chg = mgr.propose_change(title="contested", created_by="lumina", implementer="lumina")
    mgr.update_change(chg.id, agent="attacker", new_status="approved", note="no vote cast")
    assert mgr.gtd_texts == [], "precondition: the bypass attempt emitted nothing"

    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr.published.clear()

    folded = mgr.update_change(chg.id, agent="human", new_status="approved", note="CAB ok")

    assert folded.status.value == "approved"
    assert "itil.change.approved" in mgr.published
    assert mgr.gtd_texts == [f"[ITIL:{chg.id}] Implement: contested"]
