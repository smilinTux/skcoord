"""``CardStore.fold`` must tell its caller when it answered over a damaged record.

PR #125 made ``CardEventLog.read_all`` report the lines it refuses. It reports
them to a *log*, and then ``load_legacy_mutations`` builds a throwaway
``CardEventLog``, takes the events and drops ``rejected`` on the floor. So the
fold still returned a confident ``Card`` with no way for any caller to ask
whether it was computed from the whole record or from 54 events less than the
whole record. Scraping log output is not an answer a consumer can act on.

Measured on the chi fleet 2026-09-19, the three still-silent paths this covers:

* ``coordination/card_events/*.jsonl`` unreadable lines: 55 (54 in chiap02.jsonl
  from a pretty-printed block at line 1894, 1 prose line in chiap08.jsonl).
* overlay events with an action outside ``_OVERLAY_TO_STORE_ACTION`` -- the
  known ``verdict`` invisibility. Counting it here does NOT make it fold; it
  makes the discard observable instead of undetectable.
* ``cards/<id>/events/*.jsonl`` rows that parse, so the fail-closed JSON guard
  never fires, but carry no ``action`` key and so match no fold branch: 6 rows,
  every one of them a review verdict in an invented schema.

Fail loud, not fatal: nothing here raises. A hard failure on one bad line in a
Syncthing-replicated fleet ledger would take every host's board down at once.
"""

from __future__ import annotations

import json

from skcoord.card import CardEvent, CardEventLog
from skcoord.card_store import CardCore, CardStore, load_legacy_mutations


def _overlay(home, name: str, text: str) -> None:
    d = home / "coordination" / "card_events"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def _good(card_id: str, **kw) -> str:
    return json.dumps(
        {
            "card_id": card_id,
            "action": "link",
            "writer": "w",
            "ts": "2026-09-01T00:00:00+00:00",
            "seq": 0,
            "link_key": "pr",
            "link_value": "https://example.invalid/1",
            **kw,
        }
    )


def _card(store: CardStore, card_id: str) -> None:
    store.create(CardCore(id=card_id, title="Card"))


def test_fold_reports_unreadable_overlay_lines_with_shard_and_line(tmp_path):
    """The core deliverable: the count, the shard and the line reach the caller."""
    store = CardStore(tmp_path)
    _card(store, "6097241e")
    pretty = json.dumps(json.loads(_good("6097241e")), indent=2)
    _overlay(tmp_path, "chiap02.jsonl", _good("6097241e") + "\n" + pretty + "\n")

    card = store.fold("6097241e")

    assert card is not None, "fail loud, not fatal: the fold still answers"
    assert store.dropped, "the fold must not report a damaged record as complete"
    assert len(store.dropped) == pretty.count("\n") + 1
    one = store.dropped[0]
    assert one["source"] == "overlay"
    assert one["file"] == "chiap02.jsonl"
    assert one["line"] == 2, "the physical line number, so the line can be found"
    assert "unreadable" in one["reason"]
    assert one["excerpt"], "an excerpt, so the line can be recognised by eye"


def test_fold_over_an_undamaged_record_reports_nothing(tmp_path):
    """The other half: `dropped` empty has to actually mean complete."""
    store = CardStore(tmp_path)
    _card(store, "aaaaaaa1")
    _overlay(tmp_path, "chiap02.jsonl", _good("aaaaaaa1") + "\n")

    card = store.fold("aaaaaaa1")

    assert card is not None
    assert store.dropped == []
    assert card.links == {"pr": "https://example.invalid/1"}


def test_dropped_is_rebuilt_per_fold_and_does_not_accumulate(tmp_path):
    """`dropped` answers "was THIS fold complete", so it cannot grow per call."""
    store = CardStore(tmp_path)
    _card(store, "6097241e")
    _overlay(tmp_path, "chiap02.jsonl", "not json at all\n")

    first = list(store.fold("6097241e") and store.dropped)
    second = list(store.fold("6097241e") and store.dropped)

    assert len(first) == 1
    assert second == first, "a second fold must not double-count the same damage"


def test_fold_counts_the_overlay_action_it_cannot_map(tmp_path):
    """The known `verdict` invisibility, now observable rather than undetectable.

    This deliberately does NOT change what folds. `verdict` is still not applied
    (fixing that is a separate decision about board semantics). What changes is
    that discarding it is now reported instead of leaving no trace at all.
    """
    store = CardStore(tmp_path)
    _card(store, "6097241e")
    # Bypass CardEventLog.append, which now rejects this action outright. The
    # live shards predate that guard and still carry these rows.
    _overlay(tmp_path, "chiap02.jsonl", _good("6097241e", action="verdict") + "\n")

    card = store.fold("6097241e")

    assert card is not None
    assert card.links == {}, "unchanged: the fold still does not apply a verdict"
    assert len(store.dropped) == 1
    one = store.dropped[0]
    assert "unmapped action 'verdict'" in one["reason"]
    # read_all() returns CardEvents, which do not carry the shard they came
    # from. Naming a file here would mean inventing one, so it stays blank and
    # the writer locates the event instead.
    assert one["file"] == ""
    assert "writer=w" in one["excerpt"]
    assert "card=6097241e" in one["excerpt"]


def test_fold_reports_a_store_row_that_parses_but_carries_no_action(tmp_path):
    """The six live rows that the fail-closed JSON guard cannot see.

    ``_read_events`` raises on a line that will not parse. These parse, so they
    sail past it, then match no fold branch because every branch keys off
    ``action``.
    """
    store = CardStore(tmp_path)
    _card(store, "14f31b36")
    events = tmp_path / "cards" / "14f31b36" / "events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "pi@chiap02.jsonl").write_text(
        json.dumps(
            {
                "seq": 2,
                "agent": "pi",
                "type": "verdict",
                "timestamp": "2026-08-27T17:15:00.000000Z",
                "verdict": "PASS",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    card = store.fold("14f31b36")

    assert card is not None, "fail loud, not fatal"
    assert len(store.dropped) == 1
    one = store.dropped[0]
    assert one["source"] == "store"
    assert one["file"] == "pi@chiap02.jsonl"
    assert one["line"] == 1
    assert "no action key" in one["reason"]
    assert "type" in one["reason"], "name the keys it did carry, to identify the schema"


def test_unreadable_archive_index_line_is_reported(tmp_path):
    """The third silent path, untouched by #125: a bare `except: continue`."""
    archive = tmp_path / "coordination" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "chiap02.jsonl").write_text(
        json.dumps({"id": "aaaaaaa1", "archived_at": "2026-09-01T00:00:00+00:00"})
        + "\nnot json\n",
        encoding="utf-8",
    )

    dropped: list[dict] = []
    out = load_legacy_mutations(tmp_path, dropped=dropped)

    assert "aaaaaaa1" in out, "the readable line still folds"
    assert len(dropped) == 1
    assert dropped[0]["source"] == "archive"
    assert dropped[0]["line"] == 2
    assert "unparseable" in dropped[0]["reason"]


def test_load_legacy_mutations_without_dropped_still_works(tmp_path):
    """The parameter is opt-in; existing callers are untouched."""
    _overlay(tmp_path, "chiap02.jsonl", "garbage\n" + _good("aaaaaaa1") + "\n")

    out = load_legacy_mutations(tmp_path)

    assert list(out) == ["aaaaaaa1"]


def test_read_all_rejects_are_carried_through_to_the_fold(tmp_path):
    """The specific plumbing gap: read_all knew, load_legacy_mutations forgot."""
    _overlay(tmp_path, "chiap08.jsonl", "Independent review of 21 wheels.\n")

    log = CardEventLog(tmp_path)
    log.read_all()
    assert len(log.rejected) == 1, "read_all already knew (PR #125)"

    dropped: list[dict] = []
    load_legacy_mutations(tmp_path, dropped=dropped)
    assert len(dropped) == 1, "and now the fold's caller can know too"
    assert dropped[0]["file"] == "chiap08.jsonl"
    assert "Independent review" in dropped[0]["excerpt"]


def test_append_still_refuses_an_unfoldable_action(tmp_path):
    """Guard from #125 must survive: reporting drops is not a licence to write them."""
    import pytest

    store = CardStore(tmp_path)
    _card(store, "aaaaaaa1")
    with pytest.raises(ValueError, match="unsupported overlay action"):
        CardEventLog(tmp_path).append(
            CardEvent(
                card_id="aaaaaaa1",
                action="verdict",
                writer="w",
                ts="2026-09-01T00:00:00+00:00",
                seq=0,
            )
        )
