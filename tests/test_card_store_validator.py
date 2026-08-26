from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skcoord.card_store_validator import find_malformed_cardstore_lines


def test_reports_every_malformed_line_with_exact_byte_hash(tmp_path: Path) -> None:
    first = tmp_path / "cards" / "card-a" / "events" / "writer.jsonl"
    first.parent.mkdir(parents=True)
    first.write_bytes(b'{"ok": true}\n{"broken":}\r\n\xff\n\n')
    second = tmp_path / "cards" / "card-b" / "events" / "other.jsonl"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"not-json")

    findings = find_malformed_cardstore_lines(tmp_path)

    assert [item.as_dict() for item in findings] == [
        {
            "card": "card-a",
            "file": "card-a/events/writer.jsonl",
            "line": 2,
            "sha256": hashlib.sha256(b'{"broken":}\r\n').hexdigest(),
            "reason": "Expecting value: line 1 column 11 (char 10)",
        },
        {
            "card": "card-a",
            "file": "card-a/events/writer.jsonl",
            "line": 3,
            "sha256": hashlib.sha256(b"\xff\n").hexdigest(),
            "reason": "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        },
        {
            "card": "card-b",
            "file": "card-b/events/other.jsonl",
            "line": 1,
            "sha256": hashlib.sha256(b"not-json").hexdigest(),
            "reason": "Expecting value: line 1 column 1 (char 0)",
        },
    ]


def test_is_read_only_and_scans_only_structural_card_events(tmp_path: Path) -> None:
    event = tmp_path / "cards" / "card-a" / "events" / "writer.jsonl"
    event.parent.mkdir(parents=True)
    event.write_bytes(b"bad\n")
    evidence = tmp_path / "cards" / "card-a" / "evidence" / "writer.jsonl"
    evidence.parent.mkdir()
    evidence.write_bytes(b"also-bad\n")
    before = {
        path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    findings = find_malformed_cardstore_lines(tmp_path)

    after = {
        path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert [(item.card, item.file, item.line) for item in findings] == [
        ("card-a", "card-a/events/writer.jsonl", 1)
    ]


def test_clean_absent_and_empty_stores_have_no_findings(tmp_path: Path) -> None:
    assert find_malformed_cardstore_lines(tmp_path) == []
    event = tmp_path / "cards" / "card-a" / "events" / "writer.jsonl"
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({"event_id": "ok"}) + "\n\n", encoding="utf-8")
    assert find_malformed_cardstore_lines(tmp_path) == []


def test_does_not_follow_event_file_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"bad\n")
    events = tmp_path / "cards" / "card-a" / "events"
    events.mkdir(parents=True)
    (events / "writer.jsonl").symlink_to(outside)

    assert find_malformed_cardstore_lines(tmp_path) == []
