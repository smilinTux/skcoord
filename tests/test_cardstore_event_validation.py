from __future__ import annotations

import json

import pytest

import skcoord.card_store as card_store_module
from skcoord.card_store import CardCore, CardStore
from skcoord.lifecycle_reassessment import load_cards


def test_writer_rejects_a_serialized_non_object_before_append(tmp_path, monkeypatch):
    store = CardStore(tmp_path)
    store.create(CardCore(id="object01", title="Object event only"))
    real_dumps = json.dumps

    def serialize_as_bare_string(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("action") == "note":
            return real_dumps("worker summary")
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(card_store_module.json, "dumps", serialize_as_bare_string)

    with pytest.raises(ValueError, match="must serialize as a JSON object"):
        store.append_event("object01", "note", "worker", text="worker summary")

    event_path = next((tmp_path / "cards" / "object01" / "events").glob("*.jsonl"))
    assert event_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("action", "agent", "payload", "match"),
    [
        ("PASS_FOR_REVIEW report prose", "worker", {}, "action"),
        ("", "worker", {}, "action"),
        ("note", "", {}, "writer identity"),
        ("note", "worker", {"writer": "forged"}, "structural field"),
    ],
)
def test_invalid_structural_input_is_rejected_before_any_card_path_is_opened(
    tmp_path, monkeypatch, action, agent, payload, match
):
    store = CardStore(tmp_path)
    store.create(CardCore(id="object03", title="Input boundary"))

    def path_must_not_open(*_args, **_kwargs):
        raise AssertionError("invalid event input reached the filesystem boundary")

    monkeypatch.setattr(store, "_open_card_directory", path_must_not_open)

    with pytest.raises(ValueError, match=match):
        store.append_event("object03", action, agent, **payload)


def test_readers_and_later_appends_survive_historical_non_object_lines(tmp_path):
    store = CardStore(tmp_path)
    store.create(CardCore(id="object02", title="Preserved history"))
    first = store.append_event("object02", "note", "worker", text="valid before")
    event_path = next((tmp_path / "cards" / "object02" / "events").glob("*.jsonl"))
    with event_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps("worker summary") + "\n")
        stream.write("{pretty printed report fragment\n")

    second = store.append_event(
        "object02",
        "note",
        "worker",
        text="valid after",
        transition_id="after-damage",
    )

    assert [event["event_id"] for event in store._read_events("object02")] == [
        first["event_id"],
        second["event_id"],
    ]
    assert store.fold("object02").title == "Preserved history"
    assert [event["event_id"] for event in load_cards(tmp_path / "cards")["object02"].events] == [
        first["event_id"],
        second["event_id"],
    ]
    assert json.dumps("worker summary") in event_path.read_text(encoding="utf-8")
