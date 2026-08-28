from __future__ import annotations

import json
import multiprocessing
import socket
from pathlib import Path

import pytest

from skcoord.card import (
    CardEvent,
    CardEventAuthorityUnavailableError,
    CardEventLog,
    CardEventTransitionConflictError,
    GovernedCardEventConfig,
    StaleCardLinkRevisionError,
    derive_card_event_transition_id,
)
from skcoord.card_store import CardCore, CardStore


def _card(home: Path, card_id: str) -> None:
    CardStore(home).create(CardCore(id=card_id, title=card_id, created_by="test"))


def _governed_log(
    home: Path,
    *,
    authority_node: str = "authority-node",
    authority_epoch: str = "epoch-1",
) -> CardEventLog:
    baseline = CardEventLog(home).capture_activation_baseline()
    return CardEventLog(
        home,
        GovernedCardEventConfig(
            enabled=True,
            authority_node=authority_node,
            authority_epoch=authority_epoch,
            local_node=authority_node,
            activation_baseline=baseline,
        ),
    )


def _marker(
    card_id: str, verdict_event_id: str, value: str = "verdict-1"
) -> tuple[CardEvent, str]:
    event = CardEvent(
        card_id=card_id,
        action="add_label",
        label="blocker-now-done",
        link_key="blocker_return_marker",
        link_value=value,
        writer="sweep",
    )
    transition_id = derive_card_event_transition_id(
        card_id=card_id,
        verdict_event_id=verdict_event_id,
        action=event.action,
        label=event.label or "",
        marker_payload=event.link_value or "",
    )
    return event, transition_id


def _race_link(home: str, config_json: str, card_id: str, gate, results) -> None:
    config = GovernedCardEventConfig.model_validate_json(config_json)
    gate.wait()
    try:
        receipt = CardEventLog(Path(home), config).append(
            CardEvent(
                card_id=card_id,
                action="link",
                link_key="verdict",
                link_value="PASS",
                writer="new-review",
            )
        )
        results.put(("link", "committed", receipt.event_id))
    except Exception as exc:  # noqa: BLE001
        results.put(("link", type(exc).__name__, str(exc)))


def _race_marker(
    home: str,
    config_json: str,
    card_id: str,
    expected_revision: str,
    transition_id: str,
    gate,
    results,
) -> None:
    config = GovernedCardEventConfig.model_validate_json(config_json)
    event, _ = _marker(card_id, expected_revision)
    gate.wait()
    try:
        receipt = CardEventLog(Path(home), config).append_if_link_revision(
            event,
            expected_link_revision=expected_revision,
            transition_id=transition_id,
        )
        results.put(("marker", "committed", receipt.event_id))
    except Exception as exc:  # noqa: BLE001
        results.put(("marker", type(exc).__name__, str(exc)))


def test_historical_records_remain_readable_and_new_appends_gain_event_id(tmp_path) -> None:
    directory = tmp_path / "coordination" / "card_events"
    directory.mkdir(parents=True)
    historical = {
        "card_id": "legacy01",
        "action": "move",
        "writer": "legacy",
        "ts": "2026-01-01T00:00:00+00:00",
        "seq": 0,
        "column": "ready",
    }
    (directory / "legacy.jsonl").write_text(json.dumps(historical) + "\n")

    log = CardEventLog(tmp_path)
    assert log.read_all()[0].event_id is None
    event = CardEvent(card_id="legacy01", action="move", column="doing")
    assert log.append(event) is None
    assert event.writer == socket.gethostname()
    appended = [event for event in log.read_all() if event.event_id is not None]
    assert len(appended) == 1
    assert len(appended[0].event_id or "") == 32


def test_disabled_mode_keeps_legacy_link_path_compatible(tmp_path) -> None:
    _card(tmp_path, "disabled01")
    log = CardEventLog(tmp_path)
    intended = CardEvent(
        card_id="disabled01",
        action="link",
        link_key="verdict",
        link_value="BLOCKED",
    )
    assert log.append(intended) is None
    assert intended.writer == socket.gethostname()
    event = next(event for event in log.read_all() if event.link_value == "BLOCKED")
    assert event.authority_node is None
    assert event.authority_epoch is None
    assert event.transition_id is None


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("move", {"column": "doing"}),
        ("add_label", {"label": "compatibility"}),
        ("describe", {"description": "compatible"}),
        ("link", {"link_key": "verdict", "link_value": "BLOCKED"}),
    ],
)
def test_disabled_mode_preserves_legacy_append_contract(tmp_path, action, fields) -> None:
    _card(tmp_path, "compat001")
    log = CardEventLog(tmp_path)
    intended = CardEvent(card_id="compat001", action=action, **fields)

    assert log.append(intended) is None
    assert intended.writer == socket.gethostname()
    assert log.read_all()[-1].writer == intended.writer


def test_governed_link_and_transition_share_authority_journal(tmp_path) -> None:
    _card(tmp_path, "governed01")
    log = _governed_log(tmp_path)
    verdict = log.append(
        CardEvent(
            card_id="governed01",
            action="link",
            link_key="verdict",
            link_value="BLOCKED blocked_on=card referent=card:abcdef12",
            writer="reviewer",
        )
    )
    marker, transition_id = _marker("governed01", verdict.event_id)
    marker_receipt = log.append_if_link_revision(
        marker,
        expected_link_revision=verdict.event_id,
        transition_id=transition_id,
    )

    assert verdict.journal == "authority-node.jsonl"
    assert marker_receipt.journal == verdict.journal
    assert marker_receipt.journal_line == verdict.journal_line + 1
    assert log.audit_governed_writes().available
    events = log.read_all()
    assert {event.authority_node for event in events} == {"authority-node"}
    assert {event.authority_epoch for event in events} == {"epoch-1"}


def test_governed_write_fails_off_authority_or_without_baseline(tmp_path) -> None:
    _card(tmp_path, "authority01")
    off_authority = CardEventLog(
        tmp_path,
        GovernedCardEventConfig(
            enabled=True,
            authority_node="authority-node",
            authority_epoch="epoch-1",
            local_node="other-node",
            activation_baseline=CardEventLog(tmp_path).capture_activation_baseline(),
        ),
    )
    with pytest.raises(CardEventAuthorityUnavailableError, match="not the CardEvent authority"):
        off_authority.append(CardEvent(card_id="authority01", action="link", link_key="verdict"))

    missing_baseline = CardEventLog(
        tmp_path,
        GovernedCardEventConfig(
            enabled=True,
            authority_node="authority-node",
            authority_epoch="epoch-1",
            local_node="authority-node",
        ),
    )
    with pytest.raises(CardEventAuthorityUnavailableError, match="baseline is missing"):
        missing_baseline.append(
            CardEvent(card_id="authority01", action="link", link_key="verdict")
        )
    assert CardEventLog(tmp_path).read_all() == []


def test_transition_retry_returns_exact_receipt_and_conflicts_fail_closed(tmp_path) -> None:
    _card(tmp_path, "retry001")
    _card(tmp_path, "retry002")
    log = _governed_log(tmp_path)
    verdict = log.append(
        CardEvent(
            card_id="retry001",
            action="link",
            link_key="verdict",
            link_value="BLOCKED",
            writer="reviewer",
        )
    )
    marker, transition_id = _marker("retry001", verdict.event_id)
    first = log.append_if_link_revision(
        marker,
        expected_link_revision=verdict.event_id,
        transition_id=transition_id,
    )
    retry_marker, _ = _marker("retry001", verdict.event_id)
    retry = log.append_if_link_revision(
        retry_marker,
        expected_link_revision=verdict.event_id,
        transition_id=transition_id,
    )
    assert retry == first
    assert sum(event.transition_id == transition_id for event in log.read_all()) == 1

    changed_payload, _ = _marker("retry001", verdict.event_id, value="different")
    with pytest.raises(CardEventTransitionConflictError, match="different intent"):
        log.append_if_link_revision(
            changed_payload,
            expected_link_revision=verdict.event_id,
            transition_id=transition_id,
        )
    with pytest.raises(CardEventTransitionConflictError, match="different intent"):
        log.append_if_link_revision(
            retry_marker,
            expected_link_revision="different-verdict",
            transition_id=transition_id,
        )
    other_card_marker, _ = _marker("retry002", verdict.event_id)
    with pytest.raises(CardEventTransitionConflictError, match="different intent"):
        log.append_if_link_revision(
            other_card_marker,
            expected_link_revision=verdict.event_id,
            transition_id=transition_id,
        )

    wrong_epoch = CardEventLog(
        tmp_path,
        log.governance.model_copy(update={"authority_epoch": "epoch-2"}),
    )
    with pytest.raises(CardEventAuthorityUnavailableError, match="wrong authority epoch"):
        wrong_epoch.append_if_link_revision(
            retry_marker,
            expected_link_revision=verdict.event_id,
            transition_id=transition_id,
        )


def test_stale_verdict_rejects_without_appending_and_new_verdict_requalifies(tmp_path) -> None:
    _card(tmp_path, "stale001")
    log = _governed_log(tmp_path)
    old = log.append(
        CardEvent(
            card_id="stale001",
            action="link",
            link_key="verdict",
            link_value="BLOCKED old",
        )
    )
    new = log.append(
        CardEvent(
            card_id="stale001",
            action="link",
            link_key="verdict",
            link_value="BLOCKED new",
            ts="2025-01-01T00:00:00+00:00",
        )
    )
    old_marker, old_transition = _marker("stale001", old.event_id)
    before = len(log.read_all())
    with pytest.raises(StaleCardLinkRevisionError, match="expected link revision"):
        log.append_if_link_revision(
            old_marker,
            expected_link_revision=old.event_id,
            transition_id=old_transition,
        )
    assert len(log.read_all()) == before

    new_marker, new_transition = _marker("stale001", new.event_id)
    assert new_transition != old_transition
    receipt = log.append_if_link_revision(
        new_marker,
        expected_link_revision=new.event_id,
        transition_id=new_transition,
    )
    assert receipt.transition_id == new_transition


def test_audit_disables_capability_after_off_authority_link(tmp_path) -> None:
    _card(tmp_path, "audit001")
    log = _governed_log(tmp_path)
    verdict = log.append(
        CardEvent(
            card_id="audit001",
            action="link",
            link_key="verdict",
            link_value="BLOCKED",
        )
    )
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id="audit001",
            action="link",
            link_key="verdict",
            link_value="PASS",
            writer="rogue",
        )
    )

    audit = log.audit_governed_writes()
    assert not audit.available
    assert any("outside authority journal" in item for item in audit.violations)
    marker, transition_id = _marker("audit001", verdict.event_id)
    with pytest.raises(CardEventAuthorityUnavailableError, match="outside authority journal"):
        log.append_if_link_revision(
            marker,
            expected_link_revision=verdict.event_id,
            transition_id=transition_id,
        )


def test_concurrent_verdict_and_marker_serialize_on_one_journal(tmp_path) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires fork for a shared filesystem race")
    _card(tmp_path, "race0001")
    log = _governed_log(tmp_path)
    old = log.append(
        CardEvent(
            card_id="race0001",
            action="link",
            link_key="verdict",
            link_value="BLOCKED",
            writer="old-review",
        )
    )
    marker, transition_id = _marker("race0001", old.event_id)
    assert marker.card_id == "race0001"

    context = multiprocessing.get_context("fork")
    gate = context.Barrier(2)
    results = context.Queue()
    config_json = log.governance.model_dump_json()
    processes = [
        context.Process(
            target=_race_link,
            args=(str(tmp_path), config_json, "race0001", gate, results),
        ),
        context.Process(
            target=_race_marker,
            args=(
                str(tmp_path),
                config_json,
                "race0001",
                old.event_id,
                transition_id,
                gate,
                results,
            ),
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    outcomes = {item[0]: item[1:] for item in (results.get(timeout=2) for _ in processes)}
    assert outcomes["link"][0] == "committed"
    assert outcomes["marker"][0] in {"committed", "StaleCardLinkRevisionError"}
    events = CardEventLog(tmp_path).read_all()
    marker_index = next(
        (index for index, event in enumerate(events) if event.transition_id == transition_id),
        None,
    )
    new_link_index = next(
        index
        for index, event in enumerate(events)
        if event.action == "link" and event.link_value == "PASS"
    )
    if marker_index is not None:
        assert marker_index < new_link_index
    assert sum(event.transition_id == transition_id for event in events) <= 1
