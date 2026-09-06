"""CAB retirement keeps historical votes and hardens destructive execution."""

import json

from skcoord.itil import ITILManager


def _high_risk(mgr, **overrides):
    kwargs = {
        "title": "delete private key material",
        "risk": "high",
        "rollback_plan": "restore only from verified custody supplement",
        "test_plan": "fail closed on any custody mismatch before deletion",
        "created_by": "jarvis",
    }
    kwargs.update(overrides)
    return mgr.propose_change(**kwargs)


def test_cab_is_retired_without_rewriting_core_or_deleting_votes(tmp_path):
    mgr = ITILManager(tmp_path)
    change = _high_risk(mgr)
    vote = mgr.submit_cab_vote(change.id, "jarvis", decision="approved")

    folded = mgr.list_changes()[0]
    core = json.loads((mgr.changes_dir / change.id / "core.json").read_text())

    assert folded.status.value == "proposed"
    assert folded.cab_required is True
    assert core["cab_required"] is True
    assert any(item["action"] == "cab-retired" for item in folded.timeline)
    assert (mgr.cab_dir / f"{change.id}-{vote.agent}.json").exists()


def test_high_risk_requires_plans_and_passing_preflight(tmp_path):
    mgr = ITILManager(tmp_path)
    change = _high_risk(mgr, rollback_plan="", test_plan="")

    mgr.update_change(change.id, "executor", new_status="approved")
    folded = mgr.update_change(change.id, "executor", new_status="implementing")
    refusal = folded.timeline[-1]

    assert folded.status.value == "approved"
    assert refusal["conflicted"] is True
    assert "preflight plan required" in refusal["conflict_reason"]
    assert "rollback plan required" in refusal["conflict_reason"]


def test_failed_preflight_stops_before_implementing_and_says_why(tmp_path):
    mgr = ITILManager(tmp_path)
    change = _high_risk(mgr)

    mgr.update_change(change.id, "executor", new_status="approved")
    mgr.record_change_preflight(
        change.id,
        "executor",
        passed=False,
        reason="custody fingerprint mismatch",
    )
    folded = mgr.update_change(change.id, "executor", new_status="implementing")

    assert folded.status.value == "approved"
    assert folded.preflight["passed"] is False
    assert "custody fingerprint mismatch" in folded.timeline[-1]["conflict_reason"]
    assert not any(
        item["action"].endswith("->implementing") and not item.get("conflicted")
        for item in folded.timeline
    )


def test_latest_passing_preflight_allows_high_risk_execution(tmp_path):
    mgr = ITILManager(tmp_path)
    change = _high_risk(mgr)

    mgr.update_change(change.id, "executor", new_status="approved")
    mgr.record_change_preflight(change.id, "executor", passed=False, reason="custody mismatch")
    mgr.record_change_preflight(change.id, "executor", passed=True, reason="custody verified")
    folded = mgr.update_change(change.id, "executor", new_status="implementing")

    assert folded.status.value == "implementing"


def test_failed_preflight_requires_reason(tmp_path):
    mgr = ITILManager(tmp_path)
    change = _high_risk(mgr)

    try:
        mgr.record_change_preflight(change.id, "executor", passed=False, reason=" ")
    except ValueError as exc:
        assert "must say why" in str(exc)
    else:
        raise AssertionError("reasonless failed preflight was accepted")
