"""Crash-safe Joule mint outbox and reconciler.

CardStore lifecycle records and mint evidence are intentionally separate: a
completion does not imply that money was minted. A mint_intent is durable and
is consumed only after the wallet operation succeeds.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Any

from .card_store import CardStore, card_mutation_lock

logger = logging.getLogger(__name__)


def reconcile_joule_outbox(
    home: Path,
    mint: Callable[[str, int, str], Any] | None = None,
    agent: str = "reconciler",
) -> list[dict]:
    """Drain unexecuted intents, returning the emitted mint_executed events.

    ``mint`` receives task_id, amount, completed_by. Production defaults to
    SKJoule's engine; tests may inject a durable fake. Existing execution
    evidence is checked by task-id before invoking it, making retries safe.
    """
    store = CardStore(home)
    emitted: list[dict] = []
    for card_id in store.list_card_ids():
        # Hold the same per-card lock across the evidence check, wallet call, and
        # execution evidence append. This prevents two reconciler processes from
        # both observing an outstanding task_id and double-minting it.
        with card_mutation_lock(home, card_id):
            events = store._read_events(card_id)
            executed = {
                str(e.get("task_id")) for e in events if e.get("action") == "mint_executed"
            }
            for intent in (e for e in events if e.get("action") == "mint_intent"):
                task_id = str(intent.get("task_id", card_id))
                if task_id in executed:
                    continue
                amount = int(intent.get("joule_amount", 0))
                completed_by = str(intent.get("completed_by", intent.get("agent", "")))
                if amount <= 0 or not completed_by:
                    logger.error("invalid Joule mint intent for %s", card_id)
                    continue
                if mint is None:
                    from skcapstone.skjoule import JouleEngine
                    task = {"id": task_id, "completed_by": completed_by,
                            "priority": "medium", "title": task_id, "tags": ["community"]}
                    record = JouleEngine().auto_tokenize_task(task)
                    if record is None:
                        continue
                else:
                    mint(task_id, amount, completed_by)
                event = store.append_event(
                    card_id, "mint_executed", agent,
                    task_id=task_id, joule_amount=amount, completed_by=completed_by,
                    transition_id="mint-executed-" + str(intent.get("event_id", task_id)),
                )
                emitted.append(event)
                executed.add(task_id)
    return emitted


def backfill_joule_intents(home: Path, agent: str = "backfill") -> int:
    """Emit intents for completed cards that have no mint evidence."""
    store = CardStore(home)
    count = 0
    for card_id in store.list_card_ids():
        events = store._read_events(card_id)
        if not any(e.get("action") == "complete" for e in events):
            continue
        if any(e.get("action") in {"mint_intent", "mint_executed"} for e in events):
            continue
        complete = next(e for e in reversed(events) if e.get("action") == "complete")
        card = store.fold(card_id)
        priority = str(getattr(card, "priority", "medium")) if card else "medium"
        amount = {"critical": 500, "high": 100, "medium": 50, "low": 25}.get(priority, 50)
        store.append_event(card_id, "mint_intent", agent, task_id=card_id,
                           completed_by=str(complete.get("agent", agent)), joule_amount=amount,
                           transition_id="backfill-mint-intent-" + card_id)
        count += 1
    return count
