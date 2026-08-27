"""Controlled evidence key vocabulary and historical safety regression tests."""

import json
from pathlib import Path

import pytest

from skcoord.card import CardEvent, CardEventLog, fold_overlay
from skcoord.card_store import CardCore, CardStore
from skcoord.evidence_vocab import CORE, canonical_key, read_links, validate_for_write


def _migration_map() -> dict:
    path = Path(__file__).parents[1] / "schemas" / "evidence-key-map.v1.json"
    return json.loads(path.read_text(encoding="utf-8"))["map"]


def test_migration_map_covers_snapshot_and_known_failure_variants() -> None:
    migration = _migration_map()
    assert len(migration) == 3674
    assert all(row["canonical"] in CORE or row["canonical"].startswith("x-legacy-")
               for row in migration.values())
    assert sum(row["canonical"] == "superseded_by" for row in migration.values()) == 22
    assert sum(row["canonical"] == "verdict" for row in migration.values()) == 43


def test_reader_finds_all_22_supersession_and_43_verdict_spellings(tmp_path) -> None:
    migration = _migration_map()
    supersession = [key for key, row in migration.items()
                    if row["canonical"] == "superseded_by"]
    verdict = [key for key, row in migration.items() if row["canonical"] == "verdict"]
    events = []
    for index, key in enumerate(supersession + verdict):
        events.append({"card_id": "known-failure", "action": "link", "link_key": key,
                       "link_value": f"value-{index}", "ts": f"2026-08-26T00:00:{index:02d}Z",
                       "writer": "fixture", "event_id": str(index)})
    path = tmp_path / "fixture.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    links = read_links([path], "known-failure")
    assert len(links["superseded_by"]) == 22
    assert len(links["verdict"]) == 43


@pytest.mark.parametrize("key", [
    "unknown_fact", "review-verdict", "verdict-20260826T021646Z",
    "evidence_0123456789abcdef0123456789abcdef",
])
def test_write_validator_rejects_uncontrolled_alias_timestamp_and_hash(key) -> None:
    with pytest.raises(ValueError, match="invalid link_key"):
        validate_for_write(key)


def test_write_validator_accepts_core_and_namespaced_escape() -> None:
    assert validate_for_write("verdict") == "verdict"
    assert validate_for_write("x-agent-card_fact") == "x-agent-card_fact"


def test_overlay_writer_rejects_before_append_and_reader_normalizes(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="vocab001", title="vocabulary"))
    log = CardEventLog(tmp_path)
    with pytest.raises(ValueError, match="timestamp in key name"):
        log.append(CardEvent(card_id="vocab001", action="link",
                             link_key="verdict-20260826T021646Z", link_value="PASS"))
    assert not (tmp_path / "coordination" / "card_events").exists()

    patch = fold_overlay([
        CardEvent(card_id="vocab001", action="link", link_key="superseded-by",
                  link_value="new-card")
    ])
    assert patch["vocab001"]["links"] == {"superseded_by": "new-card"}


def test_cardstore_writer_rejects_uncontrolled_key_and_fold_normalizes_legacy(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="vocab002", title="vocabulary"))
    with pytest.raises(ValueError, match="uncontrolled key"):
        store.append_event("vocab002", "link", "agent", link_key="agent_fact", link_value="x")
    assert store._read_events("vocab002") == []

    store.append_event("vocab002", "link", "agent", link_key="verdict", link_value="PASS")
    assert store.fold("vocab002").links == {"verdict": "PASS"}


def test_canonical_safety_concepts() -> None:
    assert canonical_key("supersedes") == "superseded_by"
    assert canonical_key("replaces") == "superseded_by"
    assert canonical_key("review-decision") == "verdict"
    assert canonical_key("result") == "verdict"
