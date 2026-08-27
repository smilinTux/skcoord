"""Terminal-lifecycle preservation across the CAB provenance upgrade.

Commit 63a18b2 ("bind CAB approval to authenticated human roles") added
``subject_role`` / ``subject_fingerprint`` / ``authorization_id`` to
``CABDecision`` and required an authenticated human identity or role for an
approval to count. Votes recorded before that upgrade carry none of those
fields, so already-terminal historical changes (e.g. chg-a76c0aee, whose
jarvis vote cites the human owner's authorization in ``conditions`` and whose
event log carries valid deployed -> verified -> closed transitions) demoted
back to ``reviewing`` at fold time.

The compatibility rule (``_LEGACY_CAB_PROVENANCE_CUTOFF`` +
``_is_legacy_unprovenanced_approval``) honors an unprovenanced approval vote
decided before the cutoff as a historical human approval. The cutoff is a
fixed past timestamp, so no vote written through ``submit_cab_vote()`` after
the fact can ever satisfy it, and the raw-status CAB bypass guard stays
fail-closed for live events. No vote is fabricated or backdated: the rule is
a pure fold-time derivation over the existing, untouched evidence.

The fixture under ``tests/fixtures/itil-terminal-legacy/`` is a verbatim copy
of the live chg-a76c0aee record (core, both writer event logs, and the CAB
vote) taken read-only from the production tree; history is not rewritten.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

import pytest

from skcoord.itil import (
    _LEGACY_CAB_PROVENANCE_CUTOFF,
    CABDecision,
    ITILManager,
    _is_legacy_unprovenanced_approval,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "itil-terminal-legacy"

# The jarvis seq-19 event: the first, authoritative verified -> closed edge.
CLOSE_TS = "2026-08-21T03:26:14.354111+00:00"


@pytest.fixture(autouse=True)
def _no_skcapstone_module_leak():
    """Mirror of the fixture in ``test_cab_bypass_fold.py`` (same rationale)."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "skcapstone" or name.startswith("skcapstone."):
            del sys.modules[name]


@pytest.fixture()
def mgr() -> ITILManager:
    return ITILManager(pathlib.Path(tempfile.mkdtemp()))


@pytest.fixture()
def legacy_mgr(mgr) -> ITILManager:
    """An ITILManager over a throwaway copy of the chg-a76c0aee fixture."""
    shutil.copytree(FIXTURE / "coordination", mgr.home / "coordination")
    return mgr


def _folded_change(mgr: ITILManager, change_id: str = "chg-a76c0aee"):
    return next(c for c in mgr.list_changes() if c.id == change_id)


def test_historical_terminal_change_folds_closed(legacy_mgr):
    """Acceptance 1: the historical approval + closed events fold closed."""
    folded = _folded_change(legacy_mgr)

    assert folded.status.value == "closed"


def test_event_19_is_the_authoritative_close(legacy_mgr):
    """The seq-19 verified -> closed edge is accepted, not conflicted."""
    folded = _folded_change(legacy_mgr)

    closes = [
        t
        for t in folded.timeline
        if t["action"] == "status:verified->closed" and not t.get("conflicted")
    ]
    assert len(closes) == 1
    assert closes[0]["ts"] == CLOSE_TS


def test_post_close_evidence_cannot_reopen_terminal_change(legacy_mgr):
    """Events 19-20 and the codex-root post-close evidence stay authoritative.

    Every event after the authoritative close (the seq-20 duplicate close,
    the codex-root post-close close, the accidental seq-21 implementing, and
    the seq-22 corrective close) must fold conflicted: closed is terminal and
    no late event reopens it.
    """
    folded = _folded_change(legacy_mgr)

    late = [t for t in folded.timeline if t["ts"] > CLOSE_TS]
    assert late, "the fixture must contain post-close evidence"
    assert all(t.get("conflicted") for t in late)
    reopens = [
        t
        for t in late
        if t["action"].endswith("->implementing") and not t.get("conflicted")
    ]
    assert not reopens


def test_new_change_cannot_self_approve_via_legacy_shaped_vote(mgr):
    """Acceptance 2 (vote route): a new unprovenanced vote never qualifies.

    Same shape as the historical jarvis vote - AI agent, human authorization
    cited in ``conditions``, no provenance fields - but ``submit_cab_vote``
    stamps ``decided_at`` with the wall-clock now, which is at/after the
    cutoff, so the legacy clause cannot apply.
    """
    chg = mgr.propose_change(title="fresh change", created_by="jarvis")

    mgr.submit_cab_vote(
        chg.id,
        agent="jarvis",
        decision="approved",
        conditions="Authorized by the human owner in the active session.",
    )

    assert mgr.list_changes()[0].status.value == "proposed"


def test_new_change_cannot_self_approve_via_raw_terminal_chain(mgr):
    """Acceptance 2 (raw-status route): the bypass guard stays fail-closed.

    Even replaying the full historical lifecycle shape (approved, then
    implementing/deployed/verified/closed with notes) as raw status events
    on a new change folds every step conflicted.
    """
    chg = mgr.propose_change(title="bypass attempt", created_by="attacker")

    for to, note in (
        ("approved", "claiming human approval"),
        ("implementing", "go"),
        ("deployed", "done"),
        ("verified", "smoke checks pass"),
        ("closed", "closing out"),
    ):
        folded = mgr.update_change(chg.id, agent="attacker", new_status=to, note=note)

    assert folded.status.value == "proposed"
    conflicts = [t for t in folded.timeline if t.get("conflicted")]
    assert any(
        "CAB approval required" in t.get("conflict_reason", "") for t in conflicts
    )


def test_post_cutoff_historical_shape_still_demotes(legacy_mgr):
    """The same record with a post-cutoff vote stays demoted (guard intact).

    Moves the fixture vote's ``decided_at`` past the cutoff in the throwaway
    copy only - proving the cutoff, not the record's content, is what gates
    the legacy clause.
    """
    vote_path = legacy_mgr.cab_dir / "chg-a76c0aee-jarvis.json"
    vote = json.loads(vote_path.read_text(encoding="utf-8"))
    vote["decided_at"] = "2026-08-21T15:54:46.863110+00:00"
    vote_path.write_text(json.dumps(vote, indent=2), encoding="utf-8")

    assert _folded_change(legacy_mgr).status.value == "reviewing"


def test_legacy_clause_requires_empty_provenance_fields():
    """A non-qualifying role (or any provenance) opts out of the legacy clause."""
    base = {
        "change_id": "chg-x",
        "agent": "jarvis",
        "decision": "approved",
        "decided_at": "2026-08-20T22:07:08.455062+00:00",
    }
    assert _is_legacy_unprovenanced_approval(CABDecision(**base))
    for field in ("subject_role", "subject_fingerprint", "authorization_id"):
        vote = CABDecision(**{**base, field: "implementer"})
        assert not _is_legacy_unprovenanced_approval(vote)


def test_legacy_clause_fails_closed_on_bad_timestamps():
    """Naive, malformed, or post-cutoff timestamps never qualify."""
    base = {"change_id": "chg-x", "agent": "jarvis", "decision": "approved"}
    for decided_at in (
        "2026-08-20T22:07:08.455062",  # naive: not provably pre-upgrade
        "not-a-timestamp",
        _LEGACY_CAB_PROVENANCE_CUTOFF,  # the boundary itself is excluded
    ):
        vote = CABDecision(**{**base, "decided_at": decided_at})
        assert not _is_legacy_unprovenanced_approval(vote)
