"""Hash-chain verification for ITIL event logs (card 887643f5).

Covers the ITIL-side scope items:
- ``_append_event`` writes prev_hash = sha256 of the previous line.
- ``_read_events`` verifies the chain and keeps events up to the last
  verified line when a break is detected.
- Legacy events without prev_hash still pass (backward compatibility).
- Tamper and truncation are detected at fold time.
"""
import hashlib
import json

import pytest

from skcoord.itil import ITILManager


@pytest.fixture()
def itil(tmp_path) -> ITILManager:
    mgr = ITILManager(tmp_path)
    mgr.ensure_dirs()
    return mgr


def _last_writer_file(mgr: ITILManager, record_id: str):
    files = sorted((mgr.incidents_dir / record_id / "events").glob("*.jsonl"))
    assert files, "no ITIL event files"
    return files[-1]


def test_itil_append_includes_prev_hash(itil):
    itil.create_incident(title="Chain incident", managed_by="alice")
    itil.update_incident(itil.list_incidents()[0].id, agent="alice", note="second")
    rec_id = itil.list_incidents()[0].id
    events = itil._read_events(itil.incidents_dir, rec_id)
    assert len(events) >= 2
    assert "prev_hash" in events[-1]
    # First event has empty prev_hash; second links to the first line's hash.
    assert events[0].get("prev_hash") in ("", None)
    assert events[1]["prev_hash"] != ""


def test_itil_chain_links_correctly(itil):
    itil.create_incident(title="Link incident", managed_by="alice")
    rid = itil.list_incidents()[0].id
    itil.update_incident(rid, agent="alice", note="b")
    itil.update_incident(rid, agent="alice", note="c")
    events = itil._read_events(itil.incidents_dir, rid)
    assert len(events) >= 3  # created + b + c, no exception
    # Verify linkage manually: each event's prev_hash must equal
    # sha256 of the exact preceding line.
    raw = _last_writer_file(itil, rid).read_text()
    lines = [l for l in raw.splitlines() if l.strip()]
    prev = ""
    for line in lines:
        ev = json.loads(line)
        expected_prev = ev.get("prev_hash")
        if expected_prev:
            assert expected_prev == prev, "ITIL chain mismatch"
        prev = hashlib.sha256(line.encode("utf-8")).hexdigest()


def test_itil_tamper_detection(itil):
    itil.create_incident(title="Tamper incident", managed_by="alice")
    itil.update_incident(itil.list_incidents()[0].id, agent="alice", note="second")
    rid = itil.list_incidents()[0].id
    wf = _last_writer_file(itil, rid)
    lines = wf.read_text().splitlines()
    first = json.loads(lines[0])
    first["note"] = "tampered"
    lines[0] = json.dumps(first)
    wf.write_text("\n".join(lines) + "\n")

    events = itil._read_events(itil.incidents_dir, rid)
    # Break detected: the second event's prev_hash no longer matches,
    # so only the verified prefix is kept.
    assert len(events) == 1
    assert "tampered" not in json.dumps(events)


def test_itil_truncation_detection(itil):
    itil.create_incident(title="Truncate incident", managed_by="alice")
    rid = itil.list_incidents()[0].id
    itil.update_incident(rid, agent="alice", note="two")
    itil.update_incident(rid, agent="alice", note="three")
    wf = _last_writer_file(itil, rid)
    lines = wf.read_text().splitlines()
    # Remove the middle line: the third event's prev_hash now points at
    # a nonexistent line.
    wf.write_text(lines[0] + "\n" + lines[2] + "\n")

    events = itil._read_events(itil.incidents_dir, rid)
    assert len(events) == 1  # only the created event survives verification


def test_itil_legacy_events_pass(itil, tmp_path):
    itil.create_incident(title="Legacy incident", managed_by="alice")
    rid = itil.list_incidents()[0].id
    ev_dir = itil.incidents_dir / rid / "events"
    legacy_file = ev_dir / "old@node1.jsonl"
    legacy_file.write_text(
        json.dumps(
            {
                "event_id": "legacy1",
                "ts": "2026-01-01T00:00:00Z",
                "writer": "old",
                "node": "node1",
                "seq": 0,
                "kind": "note",
                "note": "legacy event without prev_hash",
            }
        )
        + "\n"
    )
    events = itil._read_events(itil.incidents_dir, rid)
    # Legacy line has no prev_hash -> passes; chained events from the
    # same file would still be verified against the legacy line's hash.
    legacy_events = [e for e in events if e.get("event_id") == "legacy1"]
    assert len(legacy_events) == 1
    assert "prev_hash" not in legacy_events[0]


def test_itil_mixed_legacy_then_chained(itil, tmp_path):
    itil.create_incident(title="Mixed incident", managed_by="alice")
    rid = itil.list_incidents()[0].id
    # Simulate a legacy file with one legacy line, then append a chained
    # line to that same file. The chained line's prev_hash must be the
    # sha256 of the legacy line (chain starts after the legacy prefix).
    ev_dir = itil.incidents_dir / rid / "events"
    mixed_file = ev_dir / "mixed@node1.jsonl"
    legacy_line = json.dumps(
        {
            "event_id": "leg",
            "ts": "2026-01-01T00:00:00Z",
            "writer": "mixed",
            "node": "node1",
            "seq": 0,
            "kind": "note",
        }
    )
    chained_line = json.dumps(
        {
            "event_id": "chained",
            "ts": "2026-01-01T00:01:00Z",
            "writer": "mixed",
            "node": "node1",
            "seq": 1,
            "kind": "note",
            "prev_hash": hashlib.sha256(legacy_line.encode("utf-8")).hexdigest(),
        }
    )
    mixed_file.write_text(legacy_line + "\n" + chained_line + "\n")

    events = itil._read_events(itil.incidents_dir, rid)
    ids = [e.get("event_id") for e in events]
    assert "leg" in ids
    assert "chained" in ids
