"""Hash-chain event log integrity tests (card 887643f5)."""
import json
from pathlib import Path

import pytest

from skcoord.card_store import CardCore, CardStore


@pytest.fixture()
def store(tmp_path: Path) -> CardStore:
    s = CardStore(tmp_path)
    s.create(CardCore(id="test01", title="Chain test", kind="task"))
    return s


def _last_writer_file(store: CardStore, card_id: str) -> Path:
    ev = Path(store.home) / "cards" / card_id / "events"
    files = sorted(ev.glob("*.jsonl"))
    assert files, "no event files"
    return files[-1]


def test_append_includes_prev_hash(store):
    store.append_event("test01", "note", "alice", body="first")
    store.append_event("test01", "note", "alice", body="second")
    events = store._read_events("test01")
    assert len(events) >= 2
    # First event may have empty prev_hash (legacy start)
    assert "prev_hash" in events[-1]
    assert events[-1]["prev_hash"] != ""


def test_chain_links_correctly(store):
    store.append_event("test01", "note", "alice", body="a")
    store.append_event("test01", "note", "alice", body="b")
    store.append_event("test01", "note", "alice", body="c")
    events = store._read_events("test01")
    # The chain should not raise
    assert len(events) >= 3


def test_tamper_detection(store):
    store.append_event("test01", "note", "alice", body="original")
    store.append_event("test01", "note", "alice", body="second")

    wf = _last_writer_file(store, "test01")
    lines = wf.read_text().splitlines()
    # Tamper: modify the first event's body
    first = json.loads(lines[0])
    first["body"] = "tampered"
    lines[0] = json.dumps(first)
    wf.write_text("\n".join(lines) + "\n")

    # Second event's prev_hash should now mismatch
    with pytest.raises(ValueError, match="chain broken"):
        store._read_events("test01")


def test_truncation_detection(store):
    store.append_event("test01", "note", "alice", body="one")
    store.append_event("test01", "note", "alice", body="two")
    store.append_event("test01", "note", "alice", body="three")

    wf = _last_writer_file(store, "test01")
    lines = wf.read_text().splitlines()
    # Truncate: remove the second line
    wf.write_text(lines[0] + "\n" + lines[2] + "\n")

    # Third event's prev_hash now points to a nonexistent second line
    with pytest.raises(ValueError, match="chain broken"):
        store._read_events("test01")


def test_legacy_unchained_events_pass(store, tmp_path):
    # Write a separate card with only legacy-format events (no prev_hash)
    store.create(CardCore(id="legacy01", title="Legacy test", kind="task"))
    wf = Path(store.home) / "cards" / "legacy01" / "events" / "old-agent.jsonl"
    wf.parent.mkdir(parents=True, exist_ok=True)
    legacy1 = {"event_id": "legacy1", "ts": "2026-01-01T00:00:00Z", "writer": "old",
               "seq": 0, "action": "note"}
    legacy2 = {"event_id": "legacy2", "ts": "2026-01-01T00:01:00Z", "writer": "old",
               "seq": 1, "action": "note"}
    wf.write_text(json.dumps(legacy1) + "\n" + json.dumps(legacy2) + "\n")

    # Should not raise - legacy events have no prev_hash
    events = store._read_events("legacy01")
    assert len(events) >= 2


def test_mixed_legacy_then_chained(store):
    # Legacy event first, then a chained event appended after it
    store.create(CardCore(id="mixed01", title="Mixed test", kind="task"))
    wf = Path(store.home) / "cards" / "mixed01" / "events" / "old-agent.jsonl"
    wf.parent.mkdir(parents=True, exist_ok=True)
    legacy = json.dumps({"event_id": "leg", "ts": "2026-01-01T00:00:00Z",
                          "writer": "old", "seq": 0, "action": "note"})
    wf.write_text(legacy + "\n")

    # Append a chained event through the normal path (different writer file)
    store.append_event("mixed01", "note", "new-agent", body="chained")
    events = store._read_events("mixed01")
    assert len(events) >= 2  # legacy + chained, no exception
