"""Tests for the SchedulerTruthV1 canonical scheduler truth contract."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore
from skcoord.scheduler_truth import (
    PRIMARY_REASONS,
    REASON_ACTIONS,
    SchedulerCardFacts,
    SchedulerTruthV1,
    _canonicalize_reason,
    _primary_reason,
    compare_shadow,
    evaluate_scheduler_truth,
)


def _facts(**overrides) -> SchedulerCardFacts:
    base = dict(
        card_id="00000001",
        kind="task",
        state="backlog",
        owner=None,
        archived=False,
        voided=False,
        labels=(),
        dependencies=(),
        dependency_states={},
        verdict=None,
        verdicts=(),
        human_gate=None,
        superseded_by=None,
        ready_at=None,
        live_facts={},
    )
    base.update(overrides)
    return SchedulerCardFacts(**base)


def _facts_with(**overrides) -> SchedulerCardFacts:
    """Build a fresh ``SchedulerCardFacts`` with overrides (immutable model)."""
    base = dict(
        card_id="00000001",
        kind="task",
        state="backlog",
        owner=None,
        archived=False,
        voided=False,
        labels=(),
        dependencies=(),
        dependency_states={},
        verdict=None,
        verdicts=(),
        human_gate=None,
        superseded_by=None,
        ready_at=None,
        live_facts={},
    )
    base.update(overrides)
    return SchedulerCardFacts(**base)


def test_reason_actions_covers_every_primary_reason():
    assert set(REASON_ACTIONS) == set(PRIMARY_REASONS)
    for reason, action in REASON_ACTIONS.items():
        assert action.strip()


def test_canonicalize_reason_folds_legacy_aliases():
    assert _canonicalize_reason("state-not-eligible") == "state_not_eligible"
    assert _canonicalize_reason("blocked") == "verdict_blocked"
    assert _canonicalize_reason("claimed") == "already_owned"
    assert _canonicalize_reason("x-jarvis-wip-limit") == "x-jarvis-wip-limit"
    assert _canonicalize_reason("totally-new-reason") == "totally-new-reason"


def test_eligible_card_has_no_primary_reason():
    assert _primary_reason(_facts()) is None


def test_state_not_eligible_wins_first():
    assert _primary_reason(_facts(state="done")) == "state_not_eligible"


def test_not_task_wins_before_state():
    assert _primary_reason(_facts(kind="incident")) == "not_task"


def test_voided_beats_archived():
    assert _primary_reason(_facts(archived=True, voided=True)) == "voided"


def test_excluded_label_from_labels():
    assert _primary_reason(_facts(labels=("do-not-claim",))) == "excluded_label"


def test_dependency_unknown_beats_incomplete():
    facts = _facts(
        dependencies=("00000002", "00000003"),
        dependency_states={"00000002": "unknown", "00000003": "done"},
    )
    assert _primary_reason(facts) == "dependency_unknown"


def test_dependency_incomplete():
    facts = _facts(
        dependencies=("00000002",),
        dependency_states={"00000002": "backlog"},
    )
    assert _primary_reason(facts) == "dependency_incomplete"


def test_verdict_blocked_from_live_facts():
    facts = _facts(live_facts={"verdict_links": {"verdict": "BLOCKED"}})
    assert _primary_reason(facts) == "verdict_blocked"


def test_already_owned():
    assert _primary_reason(_facts(owner="jarvis")) == "already_owned"


def test_human_gate_open():
    assert _primary_reason(_facts(human_gate="pending")) == "human_gate_open"


def test_superseded():
    assert _primary_reason(_facts(superseded_by="00000099")) == "superseded"


def test_not_ready_time_future():
    now = datetime.now(timezone.utc)
    future = now.replace(year=now.year + 1).isoformat()
    facts = _facts(ready_at=future)
    assert _primary_reason(facts, as_of=now) == "not_ready_time"


def test_scheduler_truth_v1_population_invariant():
    card = _facts()
    truth = SchedulerTruthV1(
        generated_at="2026-09-02T00:00:00+00:00",
        population=1,
        ready_count=1,
        reason_counts={},
        cards=(card,),
        reason_actions=dict(REASON_ACTIONS),
    )
    assert truth.population == truth.ready_count + sum(truth.reason_counts.values())


def test_scheduler_truth_v1_rejects_mismatched_population():
    card = _facts()
    with pytest.raises(ValueError):
        SchedulerTruthV1(
            generated_at="2026-09-02T00:00:00+00:00",
            population=5,
            ready_count=1,
            reason_counts={},
            cards=(card,),
            reason_actions=dict(REASON_ACTIONS),
        )


def test_evaluate_scheduler_truth_live_store(tmp_path):
    store = CardStore(tmp_path)
    card_ids = ("source01", "review01", "review02")
    for card_id in card_ids:
        store.create(CardCore(id=card_id, title=card_id))
    store.append_event("review01", "complete", "reviewer")
    store.append_event("review02", "complete", "reviewer")

    truth = evaluate_scheduler_truth(tmp_path, cards=card_ids)
    assert truth.population == truth.ready_count + sum(truth.reason_counts.values())
    assert truth.contract_version == "skcoord.scheduler-truth/v1"
    assert len(truth.cards) == 3
    # review01 and review02 are completed review cards (state=done):
    # state_not_eligible is the exclusive primary reason, and the population
    # invariant must hold.
    assert truth.reason_counts.get("state_not_eligible", 0) >= 2


def test_verdict_blocked_from_overlaid_truth():
    facts = _facts_with(verdict="BLOCKED")
    assert _primary_reason(facts) == "verdict_blocked"


def test_scheduler_truth_cli_module():
    # The read-only JSON CLI must be reachable as
    # ``python -m skcoord.scheduler_truth``. The package re-exports the
    # SchedulerTruthV1 API lazily from ``_scheduler_truth_impl``.
    import importlib

    pkg = importlib.import_module("skcoord.scheduler_truth")
    cli = importlib.import_module("skcoord.scheduler_truth_cli")
    impl = importlib.import_module("skcoord._scheduler_truth_impl")
    assert hasattr(impl, "evaluate_scheduler_truth")
    assert hasattr(cli, "main")
    # ``import skcoord.scheduler_truth`` exposes the truth API.
    assert callable(pkg.evaluate_scheduler_truth)
    assert pkg.PRIMARY_REASONS is not None


def test_compare_shadow_gate():
    home = Path.home() / ".skcapstone"

    def legacy_selector():
        return {"1f706c4a": True, "ba89b64a": False, "be36c62a": False}

    result = compare_shadow(
        home,
        legacy_selector,
        cards=["1f706c4a", "ba89b64a", "be36c62a"],
    )
    # The legacy selector still marks 1f706c4a eligible even though its
    # canonical truth says state_not_eligible (the card is in ``done``).
    # The shadow gate only passes when every mismatch is explained; here
    # the legacy decision is the stale one, so exactly one unexplained
    # decision delta is expected until the legacy selector is cut over.
    assert result.unexplained_decision_deltas >= 1
    assert not result.clean()
    assert "1f706c4a" in result.mismatches


def test_compare_shadow_gate_clean():
    home = Path.home() / ".skcapstone"

    def legacy_selector():
        # The cut-over selector agrees with the canonical truth: no card in
        # this population is ready.
        return {"1f706c4a": False, "ba89b64a": False, "be36c62a": False}

    result = compare_shadow(
        home,
        legacy_selector,
        cards=["1f706c4a", "ba89b64a", "be36c62a"],
    )
    assert result.unexplained_decision_deltas == 0
    assert result.clean()
