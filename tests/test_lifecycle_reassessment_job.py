import json
from datetime import datetime, timezone
from pathlib import Path

from skcoord.lifecycle_reassessment import assess, write_report


def _card(root: Path, card_id: str, **values) -> Path:
    directory = root / card_id
    (directory / "events").mkdir(parents=True)
    core = {
        "id": card_id,
        "kind": "task",
        "title": card_id,
        "description": "",
        "created_at": "2026-08-20T00:00:00+00:00",
        "dependencies": [],
        "initial_labels": [],
    }
    core.update(values)
    (directory / "core.json").write_text(json.dumps(core), encoding="utf-8")
    return directory


def _events(directory: Path, rows: list[dict]) -> None:
    (directory / "events" / "writer@node.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_assessment_reports_all_classes_and_exclusions(tmp_path: Path) -> None:
    cards = tmp_path / "cards"
    void = _card(cards, "void0001")
    _events(void, [{"action": "void", "ts": "2026-08-20T01:00:00+00:00"}])
    _card(cards, "dependent1", dependencies=["void0001"])

    stale = _card(cards, "stale001")
    _events(
        stale,
        [
            {"action": "claim", "owner": "worker", "ts": "2026-08-20T01:00:00+00:00"},
            {
                "action": "evidence",
                "verdict": "BLOCKED",
                "event_id": "evidence1",
                "artifact_sha256": "a" * 64,
                "ts": "2026-08-20T02:00:00+00:00",
            },
        ],
    )

    _card(cards, "unclaim1")
    _card(cards, "oldcard1")
    _card(cards, "newcard1", description="This replacement supersedes card oldcard1.")
    _card(cards, "ident001", title="[CMDB-IDENT-01][P1] stable identity")

    logs = tmp_path / "logs" / "batch"
    logs.mkdir(parents=True)
    (logs / "actions.log").write_text(
        "LAUNCHED|host|session|unclaim1\nLAUNCHED|host|session|unclaim1\n",
        encoding="utf-8",
    )

    report = assess(
        cards,
        [tmp_path / "logs"],
        now=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert report["counts"] == {
        "void_dependency_edges": 1,
        "stale_claims": 1,
        "unclaimable_cards": 1,
        "superseded_cards": 1,
        "volatile_ci_identity": 1,
        "dead_worker_claims": 0,
        # a healthy tree reports zero here. The class exists so a partial write
        # seen across the Syncthing folder is isolated instead of aborting the
        # whole assessment on every host at once.
        "unreadable_cards": 0,
    }
    assert report["excluded_card_ids"] == [
        "dependent1",
        "oldcard1",
        "stale001",
        "unclaim1",
    ]
    assert report["classes"]["stale_claims"][0]["evidence_event_id"] == "evidence1"
    assert len(report["content_sha256"]) == 64


def test_lifecycle_without_separate_blocked_evidence_is_not_stale(
    tmp_path: Path,
) -> None:
    cards = tmp_path / "cards"
    card = _card(cards, "claimed01")
    _events(
        card,
        [
            {"action": "claim", "owner": "worker", "ts": "2026-08-20T01:00:00+00:00"},
            {"action": "move", "column": "review", "ts": "2026-08-20T02:00:00+00:00"},
        ],
    )
    report = assess(cards, now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert report["classes"]["stale_claims"] == []


def test_write_report_round_trips_json(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    report = assess(tmp_path / "cards", now=datetime(2026, 8, 27, tzinfo=timezone.utc))
    write_report(report, output)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_stale_claim_is_found_in_the_separate_evidence_store(tmp_path: Path) -> None:
    """The production shape: verdict lives in card_events, not on a card event.

    The original detector read only record.events and therefore reported
    stale_claims: 0 against a live store holding cards claimed for days with
    recorded BLOCKED verdicts. Verdicts are also qualified in practice
    (BLOCKED_FAIL_CLOSED), so exact equality against "BLOCKED" matched none.
    """
    cards = tmp_path / "cards"
    card = _card(cards, "evid0001")
    _events(
        card,
        [
            {
                "action": "claim",
                "owner": "codex-deploy",
                "ts": "2026-08-20T01:00:00+00:00",
            }
        ],
    )

    evidence = tmp_path / "card_events"
    evidence.mkdir(parents=True)
    (evidence / "node.jsonl").write_text(
        json.dumps(
            {
                "card_id": "evid0001",
                "action": "link",
                "writer": "codex-deploy",
                "ts": "2026-08-20T02:00:00+00:00",
                "event_id": "eve1",
                "link_key": "disposition",
                "link_value": "BLOCKED_FAIL_CLOSED: absent qualified verifier",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = assess(
        cards,
        [],
        now=datetime(2026, 8, 25, tzinfo=timezone.utc),
        evidence_dir=evidence,
    )
    rows = report["classes"]["stale_claims"]
    assert [row["card_id"] for row in rows] == ["evid0001"]
    assert rows[0]["evidence_source"] == "evidence_store"
    assert rows[0]["verdict"].startswith("BLOCKED_FAIL_CLOSED")


def test_unassign_clears_a_claim_so_it_is_not_stale(tmp_path: Path) -> None:
    """unassign ends a claim exactly as release_claim does.

    A reader that tracks only claim/release_claim/complete/void reports an
    unassigned card as still claimed, which both fabricates stale claims and
    hides the card from any pool that skips claimed cards.
    """
    cards = tmp_path / "cards"
    card = _card(cards, "unas0001")
    _events(
        card,
        [
            {
                "action": "claim",
                "owner": "codex-deploy",
                "ts": "2026-08-20T01:00:00+00:00",
            },
            {"action": "unassign", "ts": "2026-08-21T01:00:00+00:00"},
        ],
    )
    report = assess(cards, [], now=datetime(2026, 8, 25, tzinfo=timezone.utc))
    assert report["classes"]["stale_claims"] == []
    assert report["classes"]["dead_worker_claims"] == []


def test_named_agent_claim_is_never_a_dead_worker(tmp_path: Path) -> None:
    """jarvis and lumina hold claims deliberately; only ephemeral workers die."""
    cards = tmp_path / "cards"
    _events(
        _card(cards, "named001"),
        [
            {"action": "claim", "owner": "jarvis", "ts": "2026-08-20T01:00:00+00:00"},
        ],
    )
    _events(
        _card(cards, "ephem001"),
        [
            {
                "action": "claim",
                "owner": "pi-auto-ephem001",
                "ts": "2026-08-20T01:00:00+00:00",
            },
        ],
    )
    report = assess(cards, [], now=datetime(2026, 8, 25, tzinfo=timezone.utc))
    dead = [row["card_id"] for row in report["classes"]["dead_worker_claims"]]
    assert dead == ["ephem001"]
