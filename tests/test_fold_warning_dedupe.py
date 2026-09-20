"""One unreadable overlay line must cost one warning, not one per fold.

`_MAX_WARNINGS_PER_FILE` caps warnings per fold() CALL, and fold() is called
once per card. A fleet selector cycle folds thousands of cards, so a single
malformed line produced the same warning thousands of times in one process.

Measured 2026-09-19 on the chi estate: one bad line flooded every fleet worker
log and every CLI invocation, and pushed a seat dispatcher's JSON receipt past
journald's 48KB message cap, so the receipt arrived truncated and unparseable.
Operators could not see why dispatch failed. One unreadable line cost the
observability of the whole fleet, which is far worse than the bad line itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import skcoord.card as card_mod
from skcoord.card_store import CardCore, CardStore


def _card(root: Path, card_id: str) -> None:
    CardStore(root).create(CardCore(id=card_id, title=card_id))


def _overlay(root: Path, name: str, lines: list[str]) -> Path:
    overlay = root / "coordination" / "card_events"
    overlay.mkdir(parents=True, exist_ok=True)
    path = overlay / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _count_drop_warnings(records) -> int:
    return sum(
        1 for r in records
        if "is not a card event, dropping it" in r.getMessage()
    )


def test_one_bad_line_warns_once_across_many_folds(tmp_path, caplog) -> None:
    card_mod._WARNED_LINES.clear()
    for index in range(5):
        _card(tmp_path, f"card{index}")
    # Valid JSON, but not a card event: the exact shape observed in production,
    # an event written with `event` where the schema requires `action`.
    bad = json.dumps({
        "ts": "2026-09-01T08:45:00Z", "card": "card0",
        "agent": "fixture", "event": "verdict",
        "link_key": "verdict", "link_value": "PASS",
    })
    _overlay(tmp_path, "shard.jsonl", [bad])

    store = CardStore(tmp_path)
    with caplog.at_level(logging.WARNING, logger=card_mod.logger.name):
        for index in range(5):
            store.fold(f"card{index}")

    # Five folds, one bad line, one warning. Before this fix it was five.
    assert _count_drop_warnings(caplog.records) == 1


def test_distinct_bad_lines_each_warn_once(tmp_path, caplog) -> None:
    card_mod._WARNED_LINES.clear()
    _card(tmp_path, "card0")
    bad = [
        json.dumps({"ts": "2026-09-01T08:45:0%dZ" % n, "card": "card0",
                    "agent": "fixture", "event": "verdict"})
        for n in range(3)
    ]
    _overlay(tmp_path, "shard.jsonl", bad)

    store = CardStore(tmp_path)
    with caplog.at_level(logging.WARNING, logger=card_mod.logger.name):
        store.fold("card0")
        store.fold("card0")

    # Suppressing repetition must not suppress information: three distinct bad
    # lines still report three times, once each, not once in total.
    assert _count_drop_warnings(caplog.records) == 3


def test_every_occurrence_is_still_recorded_in_rejected(tmp_path) -> None:
    card_mod._WARNED_LINES.clear()
    _card(tmp_path, "card0")
    bad = json.dumps({"ts": "2026-09-01T08:45:00Z", "card": "card0",
                      "agent": "fixture", "event": "verdict"})
    _overlay(tmp_path, "shard.jsonl", [bad])

    # The warning is deduplicated; the structured record is not. A reader that
    # wants the full picture builds its own log and must still see the line,
    # even after the warning for it has already been emitted once.
    store = CardStore(tmp_path)
    store.fold("card0")
    store.fold("card0")

    log = card_mod.CardEventLog(tmp_path)
    log.read_all()
    assert any("shard.jsonl" in str(r.get("file")) for r in log.rejected)
