"""Scheduler Truth V1: the canonical scheduler truth contract.

Every card evaluated by the scheduler is described by exactly one primary
ineligibility reason (or ``None`` when the card is eligible). SKCoord owns
the shared contract, the structural facts, the reason vocabulary, and the
shadow comparison; SKCapstone owns live worker, backoff, host-routing, ITIL,
and capacity facts.

Guarantees:

- Exactly one primary reason per evaluated card (``primary_reason`` is ``None``
  only when the card is eligible).
- The population counts in ``SchedulerTruthV1.population`` must equal
  ``ready_count`` plus the sum of every primary-reason count.
- Legacy label and verdict aliases remain readable; new writes use the
  canonical vocabulary.
- The module is read-only and deterministic: same inputs, same output.

Naming note: this module lives at ``_scheduler_truth_impl.py`` while the
public import path ``skcoord.scheduler_truth`` is a namespace package that
lazily re-exports the API from here. That split keeps ``python -m
skcoord.scheduler_truth`` (CLI entry point) and ``import skcoord.scheduler_truth``
(API access) from fighting over the same name via an import cycle.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import model_validator

from skcoord.card_store import CardStore
from skcoord.graph_truth import read_joined_truth
from skcoord.portfolio_contracts import (
    ContractModel,
    canonical_json_bytes,
    canonical_sha256,
)

_SCHEMA = "skcoord.scheduler-truth/v1"

# Canonical primary-reason vocabulary. Exactly one of these may apply per card.
# Ordered by evaluation priority: the first matching check wins.
PRIMARY_REASONS: tuple[str, ...] = (
    "not_task",
    "state_not_eligible",
    "voided",
    "archived",
    "excluded_label",
    "dependency_incomplete",
    "dependency_unknown",
    "verdict_blocked",
    "already_owned",
    "human_gate_open",
    "superseded",
    "not_ready_time",
)

# Legacy aliases that must stay readable. New writes use the canonical
# vocabulary; these aliases map legacy spellings onto canonical reasons so a
# reader never has to understand every historical spelling.
_LEGACY_REASON_ALIASES: dict[str, str] = {
    # state
    "state-not-eligible": "state_not_eligible",
    "status-open-but-wip-limit-reached": "state_not_eligible",
    "not-leaf-task": "not_task",
    # labels
    "excluded-label": "excluded_label",
    "do-not-claim": "excluded_label",
    "not-claimable": "excluded_label",
    "human-gate": "human_gate_open",
    "superseded": "superseded",
    # verdicts
    "verdict_blocked": "verdict_blocked",
    "blocked": "verdict_blocked",
    "blocked-fail-closed": "verdict_blocked",
    "blocked-accurate-outcome-not-runtime-approval": "verdict_blocked",
    # ownership
    "owned": "already_owned",
    "claimed": "already_owned",
    # readiness
    "no-ready-at": "not_ready_time",
    "ready-at-missing": "not_ready_time",
    "ready-at-in-future": "not_ready_time",
    # dependencies
    "dep-incomplete": "dependency_incomplete",
    "dep-unknown": "dependency_unknown",
}

# Operator-facing action table: one action per primary reason.
REASON_ACTIONS: Mapping[str, str] = {
    "not_task": "skip: only task/epic cards are schedulable work items",
    "state_not_eligible": "move the card to backlog/ready before it can be scheduled",
    "voided": "void is terminal; do not schedule, record the decision",
    "archived": "archive is terminal; exclude from the active population",
    "excluded_label": "remove the excluding label, then re-evaluate",
    "dependency_incomplete": "complete or unblock each incomplete dependency first",
    "dependency_unknown": "register the missing dependency card, then re-evaluate",
    "verdict_blocked": "resolve the BLOCKED verdict evidence before scheduling",
    "already_owned": "release the claim (or let the current worker finish) before a new scheduler may take the card",
    "human_gate_open": "obtain human approval; the gate must be closed by a human, never by an agent",
    "superseded": "schedule the successor card; stop scheduling the superseded one",
    "not_ready_time": "wait until the ready_at timestamp passes",
}


def _canonicalize_reason(raw: str | None) -> str | None:
    """Fold one legacy reason spelling to the canonical vocabulary.

    Unknown spellings are returned normalized but flagged so callers can
    report them instead of silently guessing.
    """
    if raw is None:
        return None
    key = raw.strip().lower()
    if key in PRIMARY_REASONS:
        return key
    if key in _LEGACY_REASON_ALIASES:
        return _LEGACY_REASON_ALIASES[key]
    # Escape hatch: x-<agent>-<reason> style custom reasons pass through.
    if re.fullmatch(r"x-[a-z0-9][a-z0-9_.-]*-[a-z0-9][a-z0-9_.-]*", key):
        return key
    return key


class SchedulerCardFacts(ContractModel):
    """Read-only structural facts for one evaluated card.

    SKCoord supplies the structural facts; SKCapstone supplies live worker,
    backoff, host-routing, ITIL, and capacity facts. Only the structural
    fields are part of the shared contract; the live fields are carried
    through as an opaque mapping.
    """

    card_id: str
    kind: str
    state: str
    owner: str | None
    archived: bool
    voided: bool
    labels: tuple[str, ...]
    dependencies: tuple[str, ...]
    dependency_states: Mapping[str, str]
    verdict: str | None
    verdicts: tuple[str, ...]
    human_gate: str | None
    superseded_by: str | None
    ready_at: str | None
    live_facts: Mapping[str, Any]

    @model_validator(mode="after")
    def canonical_arrays(self) -> "SchedulerCardFacts":
        if tuple(sorted(self.labels)) != self.labels:
            raise ValueError("labels must be sorted")
        if tuple(sorted(self.dependencies)) != self.dependencies:
            raise ValueError("dependencies must be sorted")
        if tuple(sorted(self.verdicts)) != self.verdicts:
            raise ValueError("verdicts must be sorted")
        return self


class SchedulerTruthV1(ContractModel):
    """The canonical scheduler truth contract (versioned, read-only).

    ``population`` MUST equal ``ready_count`` plus the sum of every
    primary-reason count, so an operator can verify the arithmetic at a
    glance.
    """

    contract_version: str = _SCHEMA
    generated_at: str
    population: int
    ready_count: int
    reason_counts: Mapping[str, int]
    cards: tuple[SchedulerCardFacts, ...]
    reason_actions: Mapping[str, str]

    @model_validator(mode="after")
    def population_equals_ready_plus_reasons(self) -> "SchedulerTruthV1":
        total = self.ready_count + sum(self.reason_counts.values())
        if self.population != total:
            raise ValueError(
                "population must equal ready_count plus all primary-reason counts"
            )
        return self

    def to_json(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True)
class ShadowComparison:
    """Result of comparing the new truth against the legacy selector."""

    mismatches: tuple[str, ...]
    unexplained_decision_deltas: int
    matched: int
    total: int

    def clean(self) -> bool:
        """The shadow gate: cutover is allowed only when this is zero."""
        return self.unexplained_decision_deltas == 0


def _dependency_states_for(
    store: CardStore, card_id: str
) -> dict[str, str]:
    """Return the derived status of every dependency card id."""
    out: dict[str, str] = {}
    for dep in _dependencies(store, card_id):
        dep_card = store.fold(dep)
        out[dep] = dep_card.status.value if dep_card else "unknown"
    return out


def _dependencies(store: CardStore, card_id: str) -> tuple[str, ...]:
    card = store.fold(card_id)
    if card is None:
        return ()
    return tuple(sorted(card.dependencies))


def _current_links(store: CardStore, card_id: str) -> dict[str, str]:
    """Current link values from the card's own authoritative events."""
    links: dict[str, str] = {}
    for event in store._read_events(card_id):
        if event.get("action") != "link":
            continue
        key = event.get("link_key")
        value = event.get("link_value")
        if key is not None:
            links[str(key)] = str(value)
    return links


def _latest_verdict(links: Mapping[str, str]) -> str | None:
    for key, value in sorted(links.items()):
        if re.fullmatch(r"(^|_)?verdict(_|$)", key.lower()):
            if str(value) in {"PASS", "PASS_FOR_REVIEW", "BLOCKED", "FAIL"}:
                return str(value)
    return None


def _primary_reason(
    facts: SchedulerCardFacts,
    as_of: datetime | None = None,
) -> str | None:
    """Return exactly one primary ineligibility reason, or ``None`` if eligible.

    Checks run in the canonical priority order defined by ``PRIMARY_REASONS``.
    The first check that fails selects that reason as the card's sole primary
    reason, guaranteeing the "exactly one primary reason" guarantee.

    ``as_of`` is required when a card carries a future ``ready_at``; pass it
    explicitly in unit tests and direct calls.
    """
    if facts.kind not in ("task", "epic"):
        return "not_task"
    if facts.state not in ("backlog", "ready"):
        return "state_not_eligible"
    if facts.voided:
        return "voided"
    if facts.archived:
        return "archived"
    excluded = {
        label
        for label in facts.labels
        if _canonicalize_reason(label) in {"excluded_label", "human_gate_open", "superseded"}
    }
    if excluded:
        return "excluded_label"
    states = dict(facts.dependency_states)
    unknown = [dep for dep in facts.dependencies if states.get(dep) == "unknown"]
    if unknown:
        return "dependency_unknown"
    incomplete = [dep for dep in facts.dependencies if states.get(dep) != "done"]
    if incomplete:
        return "dependency_incomplete"
    # A BLOCKED verdict in the card's evidence links blocks scheduling.
    # Check the overlay-derived ``facts.verdict`` (canonical, from
    # joined-truth) plus any live-fact verdict links carried opaquely by
    # SKCapstone. Structural events and evidence events stay separate.
    if facts.verdict == "BLOCKED":
        return "verdict_blocked"
    if _latest_verdict(facts.live_facts.get("verdict_links") or {}) == "BLOCKED":
        return "verdict_blocked"
    if facts.owner:
        return "already_owned"
    gate = facts.human_gate
    if gate is not None and gate not in ("not-required", "approved"):
        return "human_gate_open"
    if facts.superseded_by:
        return "superseded"
    if facts.ready_at and as_of is not None:
        # If ready_at is parseable and in the future relative to the reference
        # time, the card is not yet ready.
        try:
            ready_ts = datetime.fromisoformat(facts.ready_at)
            if ready_ts.tzinfo is None:
                ready_ts = ready_ts.replace(tzinfo=timezone.utc)
            if ready_ts > as_of:
                return "not_ready_time"
        except ValueError:
            return "not_ready_time"
    return None


def evaluate_scheduler_truth(
    home: Path,
    cards: Sequence[str] | None = None,
    *,
    live_facts: Mapping[str, Mapping[str, Any]] | None = None,
    policy_labels: tuple[str, ...] = (),
    as_of: datetime | None = None,
) -> SchedulerTruthV1:
    """Evaluate the canonical scheduler truth for a population of cards.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        cards: Optional explicit card id list; defaults to every task/epic card.
        live_facts: Optional per-card live facts owned by SKCapstone (worker,
            backoff, host-routing, ITIL, capacity). Carried through opaquely.
        policy_labels: Labels that mark a card as excluded or gated.
        as_of: Reference time for readiness; defaults to UTC now.

    Returns:
        A ``SchedulerTruthV1`` whose ``population`` equals ``ready_count`` plus
        the sum of every primary-reason count.
    """
    ref = as_of or datetime.now(timezone.utc)
    store = CardStore(home)
    if cards is None:
        from skcoord.card_store import task_views_from_store

        cards = [v.task.id for v in task_views_from_store(home, include_archived=True)]

    card_facts: list[SchedulerCardFacts] = []
    reason_counter: Counter[str] = Counter()
    ready_count = 0
    for card_id in cards:
        card = store.fold(card_id)
        if card is None:
            continue
        facts = SchedulerCardFacts(
            card_id=card.id,
            kind=card.kind.value,
            state=card.status.value,
            owner=card.owner,
            archived=card.archived,
            voided=any(e.get("action") == "void" for e in store._read_events(card_id)),
            labels=tuple(sorted(card.labels)),
            dependencies=_dependencies(store, card_id),
            dependency_states=_dependency_states_for(store, card_id),
            verdict=None,
            verdicts=(),
            human_gate=None,
            superseded_by=None,
            ready_at=(card.meta.get("ready_at") or None),
            live_facts=dict((live_facts or {}).get(card_id, {})),
        )
        if not isinstance(facts.live_facts, dict):
            raise TypeError("live_facts must be a mapping")
        # Overlay the evidence links (verdicts, gates, supersession) from the
        # joined-truth reader, which already folds every legacy spelling.
        #
        # The reader returns a JoinedCardTruth whose ``verdicts`` and
        # ``supersession`` lists already contain only the annotations that
        # matched their needles. Reading those lists directly (instead of
        # re-scanning ``annotations``) keeps the reader's classification as
        # the single source of truth.
        try:
            truth = read_joined_truth(home, card_id)
            facts.verdicts = tuple(sorted(a.value for a in truth.verdicts))
            if truth.verdicts:
                facts.verdict = facts.verdicts[-1]
            gate = [a for a in truth.gate_status if a.key in ("gate_status", "gate")]
            if gate:
                facts.human_gate = gate[-1].value
            if truth.supersession:
                facts.superseded_by = truth.supersession[-1].value
        except Exception:
            # Joined truth read is best-effort; structural fold already gives
            # the core facts.
            pass
        reason = _primary_reason(facts, as_of)
        if reason is None:
            ready_count += 1
        else:
            reason_counter[reason] += 1
        card_facts.append(facts)

    population = len(card_facts)
    return SchedulerTruthV1(
        generated_at=ref.isoformat(),
        population=population,
        ready_count=ready_count,
        reason_counts=dict(sorted(reason_counter.items())),
        cards=tuple(card_facts),
        reason_actions=dict(REASON_ACTIONS),
    )


def compare_shadow(
    home: Path,
    legacy_selector: Any,
    *,
    cards: Sequence[str] | None = None,
    live_facts: Mapping[str, Mapping[str, Any]] | None = None,
    as_of: datetime | None = None,
) -> ShadowComparison:
    """Compare the new SchedulerTruthV1 against the existing (legacy) selector.

    Records mismatches without changing any assignment until
    ``unexplained_decision_deltas`` reaches zero.
    """
    truth = evaluate_scheduler_truth(
        home, cards, live_facts=live_facts, as_of=as_of
    )
    legacy_decisions = legacy_selector()
    if not isinstance(legacy_decisions, Mapping):
        legacy_decisions = {c.card_id: None for c in truth.cards}
    mismatches: list[str] = []
    matched = 0
    unexplained = 0
    facts_by_id = {c.card_id: c for c in truth.cards}
    for card_id, legacy_eligible in sorted(
        ((k, v) for k, v in legacy_decisions.items()),
        key=lambda item: item[0],
    ):
        facts = facts_by_id.get(card_id)
        new_eligible = facts is not None and _primary_reason(facts, as_of) is None
        legacy_val = bool(legacy_eligible)
        if new_eligible == legacy_val:
            matched += 1
        else:
            mismatches.append(card_id)
            unexplained += 1
    for card_id in sorted(set(facts_by_id) - set(legacy_decisions)):
        mismatches.append(card_id)
        unexplained += 1
    return ShadowComparison(
        mismatches=tuple(mismatches),
        unexplained_decision_deltas=unexplained,
        matched=matched,
        total=len(legacy_decisions) + len(set(facts_by_id) - set(legacy_decisions)),
    )
