"""One partially-written card must not stop the whole fleet.

~/.skcapstone is a single Syncthing folder, so a core.json or an event line
written on one host is visible mid-write on the others. `assess` used to raise on
that, and the fleet rotation exits non-zero when the assessment fails, so a single
transient truncated write stopped every host at once.

Measured 2026-08-27: all five hosts logged "lifecycle reassessment failed:
Expecting property name enclosed in double quotes: line 2 column 1 (char 2)" and
launched nothing until the file happened to be re-read intact.
"""

import json

from skcoord.lifecycle_reassessment import assess, load_cards


def _card(cards_dir, cid, core=None, events=()):
    d = cards_dir / cid
    (d / "events").mkdir(parents=True)
    (d / "core.json").write_text(
        json.dumps(
            core if core is not None else {"id": cid, "kind": "task", "title": cid}
        )
    )
    if events:
        (d / "events" / "w@h.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n"
        )
    return d


# the exact byte sequence observed in production: a pretty-printed object caught
# after its opening brace and before its first key
TRUNCATED = "{\n"


def test_truncated_core_does_not_abort_the_assessment(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    _card(cards, "aaaaaaa1")
    _card(cards, "aaaaaaa2")
    (cards / "bbbbbbb1" / "events").mkdir(parents=True)
    (cards / "bbbbbbb1" / "core.json").write_text(TRUNCATED)

    report = assess(cards)

    # the healthy cards were still assessed
    assert report["counts"]["unreadable_cards"] == 1
    row = report["classes"]["unreadable_cards"][0]
    assert row["card_id"] == "bbbbbbb1"
    assert "core.json" in row["path"]
    # and the damaged card is kept OUT of assignment rather than handed to a worker
    assert "bbbbbbb1" in report["excluded_card_ids"]


def test_truncated_core_is_reported_verbatim_enough_to_act_on(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "ccccccc1" / "events").mkdir(parents=True)
    (cards / "ccccccc1" / "core.json").write_text(TRUNCATED)
    report = assess(cards)
    err = report["classes"]["unreadable_cards"][0]["error"]
    # a bare "failed" is not actionable; the parser's own message must survive
    assert "JSONDecodeError" in err or "Expecting" in err


def test_partial_final_event_line_is_skipped_not_fatal(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    d = _card(
        cards,
        "ddddddd1",
        events=[{"action": "claim", "owner": "w", "ts": "2026-08-27T00:00:00+00:00"}],
    )
    # simulate an append caught mid-flight: a valid line then a truncated one
    with (d / "events" / "w@h.jsonl").open("a") as fh:
        fh.write('{"action": "comp')

    report = assess(cards)

    assert report["counts"]["unreadable_cards"] == 0  # no card_id => not a card row
    # the card itself still parsed, and its good event survived
    records = load_cards(cards)
    assert "ddddddd1" in records
    assert [e["action"] for e in records["ddddddd1"].events] == ["claim"]


def test_healthy_tree_reports_no_unreadable_cards(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    _card(cards, "eeeeeee1")
    report = assess(cards)
    assert report["counts"]["unreadable_cards"] == 0
    assert report["classes"]["unreadable_cards"] == []


def test_core_that_is_valid_json_but_not_an_object_is_isolated(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "fffffff1" / "events").mkdir(parents=True)
    (cards / "fffffff1" / "core.json").write_text("[1, 2, 3]")
    report = assess(cards)
    assert report["counts"]["unreadable_cards"] == 1
    assert "not a JSON object" in report["classes"]["unreadable_cards"][0]["error"]


def test_report_still_hashes_deterministically_with_damage(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    _card(cards, "ggggggg1")
    (cards / "ggggggg2" / "events").mkdir(parents=True)
    (cards / "ggggggg2" / "core.json").write_text(TRUNCATED)
    fixed = __import__("datetime").datetime(
        2026, 8, 27, tzinfo=__import__("datetime").timezone.utc
    )
    a = assess(cards, now=fixed)
    b = assess(cards, now=fixed)
    assert a["content_sha256"] == b["content_sha256"]
