from __future__ import annotations

import json

import pytest

from skcoord.card_event_schema import (
    CARD_EVENT_SCHEMA_VERSION,
    join_event_evidence,
    validate_card_event,
)
from skcoord.card_store import CardCore, CardStore


def _legacy_event(**patch):
    event = {
        "event_id": "event-1",
        "ts": "2026-08-26T00:00:00+00:00",
        "writer": "legacy-agent",
        "node": "legacy-host",
        "seq": 0,
        "action": "move",
        "column": "done",
    }
    event.update(patch)
    return event


def test_historical_event_provenance_gaps_warn_without_blocking_fold(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="history1", title="Historical"))
    path = store.cards_dir / "history1" / "events" / "legacy@host.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps(_legacy_event()) + "\n", encoding="utf-8")

    assert store.fold("history1").status.value == "done"
    findings = validate_card_event(store._read_events("history1")[0])
    assert {(finding.level, finding.code) for finding in findings} == {
        ("warning", "legacy-schema-version"),
        ("warning", "missing-provenance"),
    }


def test_new_writes_emit_card_event_v1_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_HARNESS", "pi")
    store = CardStore(tmp_path)
    store.create(CardCore(id="newwrite1", title="New"))
    event = store.append_event("newwrite1", "move", "agent-7", column="doing")

    assert event["schema_version"] == CARD_EVENT_SCHEMA_VERSION
    assert event["provenance"] == {
        "host": event["node"],
        "agent_id": "agent-7",
        "harness": "pi",
    }
    assert validate_card_event(event, historical=False) == ()


def test_payload_cannot_remove_required_new_write_provenance(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="newwrite2", title="New"))
    with pytest.raises(ValueError, match="payload overrides reserved fields: provenance"):
        store.append_event("newwrite2", "move", "agent-7", provenance={})
    assert store._read_events("newwrite2") == []


def test_structural_lifecycle_and_links_never_imply_verdict():
    events = [
        _legacy_event(action="complete", verdict="PASS"),
        _legacy_event(event_id="event-2", action="link", link_value="PASS"),
    ]
    assert join_event_evidence(events) == {"event-1": (), "event-2": ()}


def test_only_separate_valid_evidence_event_joins_to_structural_event():
    structural = _legacy_event(action="complete")
    evidence = _legacy_event(
        event_id="evidence-1",
        action="evidence",
        subject_event_id="event-1",
        verdict="PASS",
    )
    orphan = _legacy_event(
        event_id="evidence-2",
        action="evidence",
        subject_event_id="absent",
        verdict="FAIL",
    )
    joined = join_event_evidence([structural, evidence, orphan])
    assert joined == {"event-1": (evidence,)}


def test_v1_evidence_event_requires_explicit_subject_and_verdict():
    event = _legacy_event(
        action="evidence",
        schema_version=CARD_EVENT_SCHEMA_VERSION,
        provenance={"host": "h", "agent_id": "a", "harness": "pi"},
    )
    findings = validate_card_event(event, historical=False)
    assert {(finding.code, finding.field) for finding in findings} == {
        ("invalid-evidence-event", "subject_event_id"),
        ("invalid-evidence-event", "verdict"),
    }
