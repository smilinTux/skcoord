"""Malformed-card isolation with evidence hash in the CardStore.

Card d55c6dd3 (SKCOORD-STATUS-BOUND-01): one unreadable card must be
reported by ID + evidence hash without crashing the whole status command or
hiding the readable cards.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcoord.card_store import CardStore


def _write_card(home: Path, card_id: str, core: dict) -> None:
    (home / "cards" / card_id).mkdir(parents=True, exist_ok=True)
    (home / "cards" / card_id / "core.json").write_text(
        json.dumps(core, indent=2), encoding="utf-8"
    )


def _core(card_id: str) -> dict:
    return {
        "id": card_id,
        "kind": "task",
        "title": f"card {card_id}",
        "description": "",
        "initial_swimlane": "feature",
        "initial_priority": "medium",
        "initial_labels": [],
        "acceptance_criteria": [],
        "dependencies": [],
        "created_by": "test",
        "created_at": "2026-08-30T00:00:00+00:00",
    }


def test_unreadable_card_is_isolated_with_evidence_hash(tmp_path: Path) -> None:
    """A corrupt core.json yields one malformed report with a SHA-256 evidence hash."""
    _write_card(tmp_path, "good0001", _core("good0001"))
    _write_card(tmp_path, "c9328739", _core("c9328739"))
    # Corrupt the c9328739 core so its fold fails; the healthy card stays readable.
    (tmp_path / "cards" / "c9328739" / "core.json").write_text(
        "{ not valid json", encoding="utf-8"
    )

    store = CardStore(tmp_path)
    cards, malformed = store.list_cards_with_evidence(degrade_unreadable=True)

    # The healthy card is still returned (not hidden), and the unreadable card
    # is surfaced as a degraded 'UNREADABLE' projection rather than dropped.
    assert {c.id for c in cards} == {"good0001", "c9328739"}
    readable_ids = {c.id for c in cards if not c.meta.get("unreadable")}
    assert readable_ids == {"good0001"}
    # The unreadable card is reported exactly once, by ID + evidence hash.
    assert len(malformed) == 1
    entry = malformed[0]
    assert entry["card_id"] == "c9328739"
    assert entry["source"] == "cards/c9328739"
    assert len(entry["evidence_sha256"]) == 64


def test_missing_core_reports_evidence_hash_of_reason(tmp_path: Path) -> None:
    """A card directory without a readable core is reported with a reason hash."""
    card_dir = tmp_path / "cards" / "missing001"
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "core.json").write_text("", encoding="utf-8")  # empty file

    store = CardStore(tmp_path)
    cards, malformed = store.list_cards_with_evidence(degrade_unreadable=True)

    # The empty-core card is surfaced as a degraded 'UNREADABLE' projection
    # AND reported in the malformed list with a reason-hash evidence value.
    assert [c.id for c in cards] == ["missing001"]
    assert cards[0].meta.get("unreadable") is True
    assert len(malformed) == 1
    assert malformed[0]["card_id"] == "missing001"
    assert "evidence_sha256" in malformed[0]
    assert len(malformed[0]["evidence_sha256"]) == 64


def test_task_views_with_malformed_returns_views_and_malformed(tmp_path: Path) -> None:
    """The bounded status payload separates views from the malformed report."""
    from skcoord.card_store import task_views_with_malformed

    _write_card(tmp_path, "good0001", _core("good0001"))
    _write_card(tmp_path, "c9328739", _core("c9328739"))
    (tmp_path / "cards" / "c9328739" / "core.json").write_text(
        "corrupt-bytes-not-json", encoding="utf-8"
    )

    views, malformed = task_views_with_malformed(tmp_path)
    # The readable card is in the views; the unreadable card's TaskView is also
    # projected (kind task) but flagged via meta. The malformed report carries
    # the ID + evidence hash.
    assert {v.task.id for v in views} == {"good0001", "c9328739"}
    assert [m["card_id"] for m in malformed] == ["c9328739"]
    assert "evidence_sha256" in malformed[0]


def test_strict_fold_still_raises(tmp_path: Path) -> None:
    """Governance callers (parity/export) keep strict all-or-nothing reads."""
    import pytest

    _write_card(tmp_path, "c9328739", _core("c9328739"))
    (tmp_path / "cards" / "c9328739" / "core.json").write_text(
        "corrupt-bytes-not-json", encoding="utf-8"
    )
    store = CardStore(tmp_path)
    with pytest.raises(ValueError):
        store.list_cards(degrade_unreadable=False)
