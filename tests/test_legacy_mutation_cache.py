"""Process-wide legacy mutation cache: reuse while unchanged, reparse on append."""

from __future__ import annotations

import json

from skcoord import card_store
from skcoord.card_store import CardStore, load_legacy_mutations_cached


def _archive_line(path, card_id, ts):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": card_id, "archived_at": ts, "archived_by": "t"}) + "\n")


def test_unchanged_files_are_parsed_once(tmp_path, monkeypatch):
    archive = tmp_path / "coordination" / "archive" / "host.jsonl"
    _archive_line(archive, "1234abcd", "2026-10-10T00:00:00")
    calls = []
    real = card_store.load_legacy_mutations
    monkeypatch.setattr(
        card_store, "load_legacy_mutations", lambda home: calls.append(home) or real(home)
    )
    first = load_legacy_mutations_cached(tmp_path)
    second = load_legacy_mutations_cached(tmp_path)
    assert first is second and len(calls) == 1
    assert first["1234abcd"][0]["action"] == "archive"


def test_an_append_invalidates_the_cache(tmp_path):
    archive = tmp_path / "coordination" / "archive" / "host.jsonl"
    _archive_line(archive, "1234abcd", "2026-10-10T00:00:00")
    assert "5678abcd" not in load_legacy_mutations_cached(tmp_path)
    _archive_line(archive, "5678abcd", "2026-10-10T00:00:01")
    assert "5678abcd" in load_legacy_mutations_cached(tmp_path)


def test_callers_cannot_mutate_the_shared_parse(tmp_path):
    archive = tmp_path / "coordination" / "archive" / "host.jsonl"
    _archive_line(archive, "1234abcd", "2026-10-10T00:00:00")
    events = CardStore(tmp_path)._legacy_events("1234abcd")
    events[0]["action"] = "tampered"
    assert CardStore(tmp_path)._legacy_events("1234abcd")[0]["action"] == "archive"
