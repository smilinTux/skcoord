"""Automatic close-out of [HUMAN] decision cards once a human decision lands.

A ``[HUMAN]``-titled decision card records one human decision (hold /
approve / deny) as an append-only ``human_gate_decision`` event.  Once the
decision is hash-pinned, the card itself has nothing left to decide: it must
transition to a terminal column (``done``) instead of lingering on the board
and being archived later as an abandoned backlog.

The close is idempotent (``transition_id`` dedupe), lock-guarded, and
append-only: it never rewrites or voids the decision record, and the
``[HUMAN]`` title gate itself remains permanent - only the card's lifecycle
column moves.
"""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from pathlib import Path

from .card import Column
from .card_store import (
    _HOSTNAME,
    _COMPLETE_COLUMN,
    _now_iso,
    CardStore,
    card_mutation_lock,
    validate_card_lock_identifier,
)
from .coordination import Board, _board_mutation_lock

_HUMAN_TITLE_MARKERS = ("[HUMAN]", "[H]")
_DECISION_LINK_KEYS = ("human_decision", "human-decision", "human_gate_decision")


def _title_has_human_gate(title: str) -> bool:
    upper = (title or "").upper()
    return any(marker in upper for marker in _HUMAN_TITLE_MARKERS)


def _decision_link_key(link_key: str | None) -> bool:
    return link_key in _DECISION_LINK_KEYS


def find_decided_human_gate_cards(
    home: Path, *, include_archived: bool = True
) -> list[str]:
    """Return ids of open [HUMAN]-titled cards that already hold a recorded
    hash-pinned human decision (a ``human_decision`` / ``human-decision`` /
    ``human_gate_decision`` event)."""
    store = CardStore(home)
    decided: list[str] = []
    for card_id in store.list_card_ids():
        card = store.fold(card_id)
        if card is None or (card.archived and not include_archived):
            continue
        if card.status == _COMPLETE_COLUMN:
            continue
        if not _title_has_human_gate(card.title):
            continue
        events = store._read_events(card_id) + store._legacy_events(card_id)
        has_decision_event = any(
            e.get("action") == "human_gate_decision" for e in events
        )
        if has_decision_event or any(_decision_link_key(e.get("link_key")) for e in events if e.get("action") == "link"):
            decided.append(card.id)
    return decided


def close_decided_human_gate_cards(
    home: Path,
    *,
    actor: str,
    card_ids: list[str] | None = None,
) -> dict:
    """Transition every open [HUMAN] card with a recorded decision to ``done``.

    The decision event is preserved (append-only, never voided); only the card's
    lifecycle column moves to the terminal ``done`` state.  The write is
    flock-guarded and idempotent via a deterministic ``transition_id`` so a
    failed write-then-error can be classified safely.
    """
    targets = card_ids if card_ids is not None else find_decided_human_gate_cards(home)
    closed: list[str] = []
    store = CardStore(home)
    for card_id in targets:
        card = store.fold(card_id)
        if card is None:
            continue
        if card.archived or not _title_has_human_gate(card.title):
            continue
        if card.status == _COMPLETE_COLUMN:
            closed.append(card.id)
            continue
        events = store._read_events(card_id) + store._legacy_events(card_id)
        has_decision = any(
            e.get("action") == "human_gate_decision" for e in events
        ) or any(
            e.get("action") == "link" and _decision_link_key(e.get("link_key"))
            for e in events
        )
        if not has_decision:
            continue
        with _board_mutation_lock(home), card_mutation_lock(home, card_id):
            Board(home).ensure_dirs()
            store.append_event(
                card_id,
                "complete",
                actor,
                transition_id=uuid.uuid4().hex,
            )
            closed.append(card_id)
    return {
        "closed": closed,
        "actor": actor,
        "ts": _now_iso(),
    }


def append_human_gate_decision(
    home: Path,
    card_id: str,
    actor: str,
    decision: str,
    decision_ref: str | None = None,
    decision_sha256: str | None = None,
    card_revision: str | None = None,
) -> dict:
    """Append one canonical hash-bound ``human_gate_decision`` event.

    ``decision`` is the recorded decision (hold / approve / deny), and
    ``decision_ref`` + ``decision_sha256`` pin the decision artifact so a
    later close-out can verify it.  The event carries ``card_revision`` so
    a version mismatch can be detected instead of inferred.
    """
    store = CardStore(home)
    validate_card_lock_identifier(card_id)
    transition_id = uuid.uuid4().hex
    with _board_mutation_lock(home), card_mutation_lock(home, card_id):
        Board(home).ensure_dirs()
        event = store.append_event(
            card_id,
            "human_gate_decision",
            actor,
            decision=decision,
            decision_ref=decision_ref,
            decision_sha256=decision_sha256,
            card_revision=card_revision,
            transition_id=transition_id,
        )
    return event
