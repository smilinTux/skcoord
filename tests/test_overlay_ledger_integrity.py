"""The overlay ledger must never lose a line in silence.

Measured 2026-09-19 on the chi fleet: ``coordination/card_events/chiap02.jsonl``
carried 54 unparseable lines (three pretty-printed JSON objects written into an
append-only JSONL log) and ``chiap08.jsonl`` carried one prose line plus one
line that was valid JSON in an invented ``card``/``agent``/``event`` schema.
Every one of them was dropped by ``CardEventLog.read_all`` through a bare
``except Exception: continue``, so the fold answered from a damaged record and
said nothing about it.

Two separate defects are covered here:

* the reader skipped bad lines with no trace at all, and
* the writer accepted an ``action`` outside the fold's vocabulary, so a
  perfectly-formed event could still vanish at fold time.
"""

from __future__ import annotations

import json
import logging

import pytest

from skcoord.card import OVERLAY_ACTIONS, CardEvent, CardEventLog

GOOD = {
    "card_id": "aaaaaaa1",
    "action": "link",
    "writer": "w",
    "ts": "2026-09-01T00:00:00+00:00",
    "seq": 0,
    "link_key": "pr",
    "link_value": "https://example.invalid/1",
}


def _events_dir(home):
    d = home / "coordination" / "card_events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(home, name, text):
    (_events_dir(home) / name).write_text(text, encoding="utf-8")


def test_pretty_printed_object_is_reported_with_file_line_and_excerpt(tmp_path, caplog):
    """The exact chiap02 shape: json.dumps(..., indent=2) into a JSONL log."""
    pretty = json.dumps(dict(GOOD, card_id="6097241e"), indent=2)
    _write(tmp_path, "chiap02.jsonl", json.dumps(GOOD) + "\n" + pretty + "\n")

    with caplog.at_level(logging.WARNING, logger="skcoord.card"):
        events = CardEventLog(tmp_path).read_all()

    assert [e.card_id for e in events] == ["aaaaaaa1"]  # the good line still loads
    blob = caplog.text
    assert "chiap02.jsonl" in blob, "the warning must name the file"
    assert "line 2" in blob, "the warning must name the line number"
    assert "{" in blob, "the warning must carry an excerpt of the rejected line"


def test_prose_line_is_reported_not_silently_skipped(tmp_path, caplog):
    """The exact chiap08 shape: a review summary written straight into the log."""
    prose = "Independent review of 21-wheel Liberty runtime candidate complete."
    _write(tmp_path, "chiap08.jsonl", json.dumps(GOOD) + "\n" + prose + "\n")

    with caplog.at_level(logging.WARNING, logger="skcoord.card"):
        events = CardEventLog(tmp_path).read_all()

    assert len(events) == 1
    assert "chiap08.jsonl" in caplog.text
    assert "Independent review" in caplog.text


def test_valid_json_in_an_invented_schema_is_reported(tmp_path, caplog):
    """chiap08 line 19798: parses as JSON, but uses card/agent/event.

    This one never reaches the JSON decoder's error path at all, so a check that
    only counts unparseable lines misses it entirely. It died in pydantic
    validation, inside the same bare ``except Exception``.
    """
    alien = json.dumps(
        {
            "ts": "2026-09-01T08:45:00Z",
            "card": "086ea05c",
            "agent": "pi-glm-chiap01-086ea05c",
            "event": "verdict",
            "link_key": "verdict",
            "link_value": "PASS",
        }
    )
    _write(tmp_path, "chiap08.jsonl", json.dumps(GOOD) + "\n" + alien + "\n")

    with caplog.at_level(logging.WARNING, logger="skcoord.card"):
        events = CardEventLog(tmp_path).read_all()

    assert len(events) == 1
    assert "chiap08.jsonl" in caplog.text


def test_rejected_lines_are_exposed_for_health_checks(tmp_path):
    """A log line alone cannot be asserted on by doctor; expose the count."""
    _write(tmp_path, "chiap02.jsonl", json.dumps(GOOD) + "\nnot json\n{\n")

    log = CardEventLog(tmp_path)
    log.read_all()

    assert len(log.rejected) == 2
    first = log.rejected[0]
    assert first["file"] == "chiap02.jsonl"
    assert first["line"] == 2
    assert "not json" in first["excerpt"]
    assert first["error"]


def test_excerpt_is_truncated_so_one_huge_line_cannot_flood_the_log(tmp_path):
    _write(tmp_path, "chiap02.jsonl", "x" * 5000 + "\n")

    log = CardEventLog(tmp_path)
    log.read_all()

    assert len(log.rejected[0]["excerpt"]) < 300


def test_a_clean_ledger_rejects_nothing_and_logs_nothing(tmp_path, caplog):
    _write(tmp_path, "chiap02.jsonl", json.dumps(GOOD) + "\n" + json.dumps(GOOD) + "\n")

    with caplog.at_level(logging.WARNING, logger="skcoord.card"):
        log = CardEventLog(tmp_path)
        events = log.read_all()

    assert len(events) == 2
    assert log.rejected == []
    assert caplog.text == ""


def test_reader_answers_from_the_surviving_record_rather_than_refusing(tmp_path):
    """Deliberate: log-and-continue, NOT fail-closed.

    The overlay is fleet-wide (one shard per host, all replicated into every
    host by Syncthing), so raising here would let one bad line written on any
    single host halt dispatch on every host. The per-card structure store
    (``CardStore._read_events``) fails closed instead, where the blast radius of
    refusing is exactly one card.
    """
    lines = [json.dumps(dict(GOOD, seq=i)) for i in range(5)]
    lines.insert(2, "corrupt")
    _write(tmp_path, "chiap02.jsonl", "\n".join(lines) + "\n")

    events = CardEventLog(tmp_path).read_all()

    assert len(events) == 5  # every good line survives the one bad neighbour


def test_append_rejects_an_action_outside_the_fold_vocabulary(tmp_path):
    """``action`` was a free-form str, so ``verdict`` wrote fine and folded to
    nothing. Reject it at the shared append point instead."""
    with pytest.raises(ValueError, match="action"):
        CardEventLog(tmp_path).append(
            CardEvent(
                card_id="6097241e",
                action="verdict",
                writer="pi-glm",
                link_key="verdict",
            )
        )


def test_a_field_containing_a_newline_still_writes_exactly_one_line(tmp_path):
    """One event is one line, even when a field value contains a newline.

    JSON escaping is what actually guarantees this, so the append point's
    single-line check is a belt-and-braces invariant rather than a reachable
    branch today. The property it protects is asserted here directly: the
    prose that landed in chiap08.jsonl was multi-line text, and if a field
    value could ever break out of its record the same way, every downstream
    line number would shift and the tail of the shard would stop parsing.
    """
    CardEventLog(tmp_path).append(
        CardEvent(card_id="aaaaaaa1", action="add_label", writer="w", label="a\nb")
    )

    shard = next((tmp_path / "coordination" / "card_events").glob("*.jsonl"))
    assert len(shard.read_text(encoding="utf-8").strip().splitlines()) == 1

    events = CardEventLog(tmp_path).read_all()
    assert [e.label for e in events] == ["a\nb"]  # round-trips intact


def test_append_still_writes_a_legitimate_event(tmp_path):
    CardEventLog(tmp_path).append(
        CardEvent(card_id="aaaaaaa1", action="add_label", writer="w", label="ok")
    )
    events = CardEventLog(tmp_path).read_all()
    assert [e.action for e in events] == ["add_label"]


def test_overlay_vocabulary_cannot_drift_from_the_fold_map():
    """If the fold learns an action and the guard does not, the guard starts
    rejecting good writes; if the guard learns one and the fold does not,
    events go silently missing again."""
    from skcoord.card_store import _OVERLAY_TO_STORE_ACTION

    assert OVERLAY_ACTIONS == frozenset(_OVERLAY_TO_STORE_ACTION)
