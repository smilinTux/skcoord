"""Generic overlay recovery is hash-pinned, reversible, and evidence preserving."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from skcoord.card import CardEvent, CardEventLog
from skcoord.card_event_recovery import (
    apply_overlay_recovery,
    plan_overlay_recovery,
    rollback_overlay_recovery,
    save_recovery_plan,
    scan_overlay_records,
)
from skcoord.card_store import CardCore, CardStore


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def malformed_overlay(tmp_path: Path) -> dict[str, object]:
    home = tmp_path / "home"
    evidence = tmp_path / "evidence"
    home.mkdir()
    evidence.mkdir()
    store = CardStore(home)
    store.create(CardCore(id="086ea05c", title="Historical review"))
    store.create(
        CardCore(
            id="f0d0ba98",
            title="Recovery authority",
            initial_owner="operator",
            initial_claim_revision="claim-revision",
        )
    )
    valid = (
        CardEvent(
            card_id="086ea05c",
            action="link",
            writer="native-reviewer",
            ts="2026-09-22T00:00:00+00:00",
            link_key="verdict",
            link_value="BLOCKED",
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    rejected = (
        json.dumps(
            {
                "ts": "2026-09-01T08:45:00Z",
                "card": "086ea05c",
                "agent": "pi-glm-chiap01-086ea05c",
                "event": "verdict",
                "link_key": "verdict",
                "link_value": "PASS - truncated and untrusted",
            }
        ).encode()
        + b"\n"
    )
    later = (
        CardEvent(
            card_id="other001",
            action="add_label",
            writer="fixture",
            ts="2026-09-22T00:00:01+00:00",
            label="healthy",
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    original = valid + rejected + later
    shard = home / "coordination" / "card_events" / "chiap08.jsonl"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(original)
    return {
        "home": home,
        "evidence": evidence,
        "store": store,
        "shard": shard,
        "original": original,
        "rejected": rejected,
        "repaired": valid + later,
    }


def _plan(fixture: dict[str, object]) -> dict:
    original = fixture["original"]
    rejected = fixture["rejected"]
    assert isinstance(original, bytes)
    assert isinstance(rejected, bytes)
    return plan_overlay_recovery(
        home=fixture["home"],
        writer="chiap08.jsonl",
        line_number=2,
        source_sha256=_sha(original),
        line_sha256=_sha(rejected),
        recovery_card_id="f0d0ba98",
        evidence=fixture["evidence"],
        actor="operator",
    )


def test_plan_is_read_only_and_binds_schema_diagnostic(
    malformed_overlay: dict[str, object],
) -> None:
    shard = malformed_overlay["shard"]
    original = malformed_overlay["original"]
    assert isinstance(shard, Path)
    plan = _plan(malformed_overlay)
    assert shard.read_bytes() == original
    assert list(Path(malformed_overlay["evidence"]).iterdir()) == []
    assert plan["line_number"] == 2
    assert plan["diagnostic"]["card_hint"] == "086ea05c"
    assert plan["diagnostic"]["action_hint"] == "verdict"
    assert plan["repaired_sha256"] == _sha(malformed_overlay["repaired"])
    assert plan["source"] == str(Path(malformed_overlay["shard"]).resolve())


def test_multi_row_plan_preserves_each_rejected_row_and_rolls_back(
    malformed_overlay: dict[str, object],
) -> None:
    original = malformed_overlay["original"]
    first_rejected = malformed_overlay["rejected"]
    shard = malformed_overlay["shard"]
    assert isinstance(original, bytes)
    assert isinstance(first_rejected, bytes)
    assert isinstance(shard, Path)
    second_rejected = (
        json.dumps(
            {
                "card_id": "second01",
                "action": "verdict",
                "writer": "legacy",
                "verdict": "BLOCKED",
            }
        ).encode()
        + b"\n"
    )
    third_rejected = (
        json.dumps(
            {
                "card_id": "third001",
                "action": "verdict",
                "writer": "legacy",
                "verdict": "PASS",
                "unexpected": "field",
            }
        ).encode()
        + b"\n"
    )
    multi_original = original + second_rejected + third_rejected
    shard.write_bytes(multi_original)

    plan = plan_overlay_recovery(
        home=malformed_overlay["home"],
        writer="chiap08.jsonl",
        line_number=(2, 4, 5),
        source_sha256=_sha(multi_original),
        line_sha256=(_sha(first_rejected), _sha(second_rejected), _sha(third_rejected)),
        recovery_card_id="f0d0ba98",
        evidence=malformed_overlay["evidence"],
        actor="operator",
    )
    assert [target["line_number"] for target in plan["targets"]] == [2, 4, 5]
    assert plan["repaired_sha256"] == _sha(malformed_overlay["repaired"])

    plan_path = save_recovery_plan(plan)
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    evidence = Path(malformed_overlay["evidence"])
    assert shard.read_bytes() == malformed_overlay["repaired"]
    assert [(evidence / name).read_bytes() for name in receipt["rejected_artifacts"]] == [
        first_rejected,
        second_rejected,
        third_rejected,
    ]
    assert (
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
        == receipt
    )

    receipt_path = evidence / receipt["receipt_artifact"]
    rollback_overlay_recovery(
        home=malformed_overlay["home"],
        receipt_path=receipt_path,
        actor="operator",
        writer_quiesced=True,
    )
    assert shard.read_bytes() == multi_original


@pytest.mark.parametrize(
    ("line_numbers", "line_hashes", "message"),
    [
        ((2, 2), ("first", "second"), "unique and strictly increasing"),
        ((3, 2), ("first", "second"), "unique and strictly increasing"),
        ((2, 3), ("first",), "counts must match"),
    ],
)
def test_multi_row_plan_rejects_unordered_duplicate_or_unpaired_targets(
    malformed_overlay: dict[str, object],
    line_numbers: tuple[int, ...],
    line_hashes: tuple[str, ...],
    message: str,
) -> None:
    original = malformed_overlay["original"]
    rejected = malformed_overlay["rejected"]
    assert isinstance(original, bytes)
    assert isinstance(rejected, bytes)
    hashes = tuple(_sha(rejected) if value == "first" else _sha(original) for value in line_hashes)
    with pytest.raises(ValueError, match=message):
        plan_overlay_recovery(
            home=malformed_overlay["home"],
            writer="chiap08.jsonl",
            line_number=line_numbers,
            source_sha256=_sha(original),
            line_sha256=hashes,
            recovery_card_id="f0d0ba98",
            evidence=malformed_overlay["evidence"],
            actor="operator",
        )


def test_multi_row_plan_rejects_an_unlisted_rejected_row(
    malformed_overlay: dict[str, object],
) -> None:
    original = malformed_overlay["original"]
    first_rejected = malformed_overlay["rejected"]
    shard = malformed_overlay["shard"]
    assert isinstance(original, bytes)
    assert isinstance(first_rejected, bytes)
    assert isinstance(shard, Path)
    second_rejected = b'{"card_id":"second","action":"verdict","writer":"legacy"}\n'
    third_rejected = b'{"card_id":"third","action":"verdict","writer":"legacy"}\n'
    changed = original + second_rejected + third_rejected
    shard.write_bytes(changed)

    with pytest.raises(ValueError, match="remains invalid at line 3"):
        plan_overlay_recovery(
            home=malformed_overlay["home"],
            writer="chiap08.jsonl",
            line_number=(2, 4),
            source_sha256=_sha(changed),
            line_sha256=(_sha(first_rejected), _sha(second_rejected)),
            recovery_card_id="f0d0ba98",
            evidence=malformed_overlay["evidence"],
            actor="operator",
        )


def test_apply_requires_explicit_writer_quiescence(
    malformed_overlay: dict[str, object],
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    with pytest.raises(ValueError, match="quiesced"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
        )


def test_apply_refuses_a_different_home_with_identical_bytes(
    malformed_overlay: dict[str, object], tmp_path: Path
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    other_home = tmp_path / "other-home"
    shutil.copytree(malformed_overlay["home"], other_home)
    with pytest.raises(ValueError, match="target path"):
        apply_overlay_recovery(
            home=other_home,
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_apply_rejects_a_resealed_plan_with_unknown_fields(
    malformed_overlay: dict[str, object],
) -> None:
    from skcoord import card_event_recovery as recovery

    plan = _plan(malformed_overlay)
    plan.pop("plan_sha256")
    plan["invented"] = "not part of the recovery schema"
    plan = recovery._seal(plan, "plan_sha256")
    plan_path = Path(malformed_overlay["evidence"]) / "invented.plan.json"
    plan_path.write_bytes(recovery._json_bytes(plan))

    with pytest.raises(ValueError, match="fields"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_apply_preserves_exact_bytes_is_retryable_and_keeps_blocked_fold(
    malformed_overlay: dict[str, object],
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    shard = malformed_overlay["shard"]
    evidence = Path(malformed_overlay["evidence"])
    assert isinstance(shard, Path)
    assert shard.read_bytes() == malformed_overlay["repaired"]
    assert (evidence / receipt["original_artifact"]).read_bytes() == malformed_overlay["original"]
    assert (evidence / receipt["rejected_artifact"]).read_bytes() == malformed_overlay["rejected"]
    assert (
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
        == receipt
    )
    card = CardStore(malformed_overlay["home"]).fold("086ea05c")
    assert card is not None
    assert card.links["verdict"] == "BLOCKED"


def test_interrupted_evidence_publication_retries_without_rewriting_source(
    malformed_overlay: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card_event_recovery as recovery

    plan_path = save_recovery_plan(_plan(malformed_overlay))
    real_publish = recovery._publish
    interrupted = False

    def stop_before_intent(directory_fd: int, name: str, raw: bytes) -> None:
        nonlocal interrupted
        if name.endswith(".intent.json") and not interrupted:
            interrupted = True
            raise OSError("synthetic interruption")
        real_publish(directory_fd, name, raw)

    monkeypatch.setattr(recovery, "_publish", stop_before_intent)
    with pytest.raises(OSError, match="synthetic interruption"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
    assert Path(malformed_overlay["shard"]).read_bytes() == malformed_overlay["original"]
    monkeypatch.setattr(recovery, "_publish", real_publish)
    assert (
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )["disposition"]
        == "applied_and_schema_verified"
    )


def test_interrupted_after_replacement_finishes_on_retry(
    malformed_overlay: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card_event_recovery as recovery

    plan_path = save_recovery_plan(_plan(malformed_overlay))
    real_replace = recovery._replace

    def replace_then_stop(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        raise OSError("synthetic interruption after replace")

    monkeypatch.setattr(recovery, "_replace", replace_then_stop)
    with pytest.raises(OSError, match="after replace"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
    assert Path(malformed_overlay["shard"]).read_bytes() == malformed_overlay["repaired"]
    monkeypatch.setattr(recovery, "_replace", real_replace)
    assert (
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )["disposition"]
        == "applied_and_schema_verified"
    )


def test_retry_rejects_a_tampered_durable_intent(
    malformed_overlay: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card_event_recovery as recovery

    plan_path = save_recovery_plan(_plan(malformed_overlay))
    real_replace = recovery._replace

    def replace_then_stop(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        raise OSError("synthetic interruption after replace")

    monkeypatch.setattr(recovery, "_replace", replace_then_stop)
    with pytest.raises(OSError, match="after replace"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
    intent = next(Path(malformed_overlay["evidence"]).glob("*.intent.json"))
    intent.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(recovery, "_replace", real_replace)
    with pytest.raises(ValueError, match="intent"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_apply_rejects_concurrent_append_without_evidence_or_replacement(
    malformed_overlay: dict[str, object],
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    shard = malformed_overlay["shard"]
    assert isinstance(shard, Path)
    shard.write_bytes(shard.read_bytes() + b"{}\n")
    changed = shard.read_bytes()
    with pytest.raises(ValueError, match="source SHA256"):
        apply_overlay_recovery(
            home=malformed_overlay["home"],
            plan_path=plan_path,
            actor="operator",
            writer_quiesced=True,
        )
    assert shard.read_bytes() == changed
    evidence = Path(malformed_overlay["evidence"])
    assert list(evidence.glob("*.original.jsonl")) == []


def test_append_waits_for_replacement_on_the_stable_writer_lock(
    malformed_overlay: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card as card_module
    from skcoord import card_event_recovery as recovery

    monkeypatch.setattr(card_module.socket, "gethostname", lambda: "chiap08")
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    real_replace = recovery._replace
    started = threading.Event()
    finished = threading.Event()
    worker: threading.Thread | None = None

    def append_after_lock() -> None:
        started.set()
        CardEventLog(malformed_overlay["home"]).append(
            CardEvent(card_id="other001", action="add_label", writer="concurrent", label="after")
        )
        finished.set()

    def replace_while_append_waits(*args, **kwargs) -> None:
        nonlocal worker
        worker = threading.Thread(target=append_after_lock)
        worker.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        assert not finished.is_set()
        real_replace(*args, **kwargs)

    monkeypatch.setattr(recovery, "_replace", replace_while_append_waits)
    apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    assert worker is not None
    worker.join(timeout=2)
    assert finished.is_set()
    events = CardEventLog(malformed_overlay["home"]).read_all()
    assert any(event.writer == "concurrent" for event in events)


def test_rollback_restores_exact_original_and_refuses_intervening_append(
    malformed_overlay: dict[str, object],
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    receipt_path = Path(malformed_overlay["evidence"]) / receipt["receipt_artifact"]
    rollback = rollback_overlay_recovery(
        home=malformed_overlay["home"],
        receipt_path=receipt_path,
        actor="operator",
        writer_quiesced=True,
    )
    shard = malformed_overlay["shard"]
    assert isinstance(shard, Path)
    assert shard.read_bytes() == malformed_overlay["original"]
    assert rollback["restored_sha256"] == _sha(malformed_overlay["original"])

    shard.write_bytes(malformed_overlay["repaired"] + b"{}\n")
    with pytest.raises(ValueError, match="intervening"):
        rollback_overlay_recovery(
            home=malformed_overlay["home"],
            receipt_path=receipt_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_rollback_requires_quiescence_and_refuses_a_different_home(
    malformed_overlay: dict[str, object], tmp_path: Path
) -> None:
    plan_path = save_recovery_plan(_plan(malformed_overlay))
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    receipt_path = Path(malformed_overlay["evidence"]) / receipt["receipt_artifact"]
    with pytest.raises(ValueError, match="quiesced"):
        rollback_overlay_recovery(
            home=malformed_overlay["home"], receipt_path=receipt_path, actor="operator"
        )

    other_home = tmp_path / "other-rollback-home"
    shutil.copytree(malformed_overlay["home"], other_home)
    with pytest.raises(ValueError, match="target path"):
        rollback_overlay_recovery(
            home=other_home,
            receipt_path=receipt_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_rollback_rejects_a_resealed_receipt_with_unknown_fields(
    malformed_overlay: dict[str, object],
) -> None:
    from skcoord import card_event_recovery as recovery

    plan_path = save_recovery_plan(_plan(malformed_overlay))
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    receipt_path = Path(malformed_overlay["evidence"]) / receipt["receipt_artifact"]
    receipt.pop("receipt_sha256")
    receipt["invented"] = "not part of the receipt schema"
    receipt = recovery._seal(receipt, "receipt_sha256")
    receipt_path.write_bytes(recovery._json_bytes(receipt))

    with pytest.raises(ValueError, match="fields"):
        rollback_overlay_recovery(
            home=malformed_overlay["home"],
            receipt_path=receipt_path,
            actor="operator",
            writer_quiesced=True,
        )


def test_interrupted_rollback_finishes_from_durable_intent(
    malformed_overlay: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card_event_recovery as recovery

    plan_path = save_recovery_plan(_plan(malformed_overlay))
    receipt = apply_overlay_recovery(
        home=malformed_overlay["home"],
        plan_path=plan_path,
        actor="operator",
        writer_quiesced=True,
    )
    receipt_path = Path(malformed_overlay["evidence"]) / receipt["receipt_artifact"]
    real_replace = recovery._replace

    def replace_then_stop(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        raise OSError("synthetic rollback interruption")

    monkeypatch.setattr(recovery, "_replace", replace_then_stop)
    with pytest.raises(OSError, match="rollback interruption"):
        rollback_overlay_recovery(
            home=malformed_overlay["home"],
            receipt_path=receipt_path,
            actor="operator",
            writer_quiesced=True,
        )
    assert Path(malformed_overlay["shard"]).read_bytes() == malformed_overlay["original"]
    monkeypatch.setattr(recovery, "_replace", real_replace)
    assert (
        rollback_overlay_recovery(
            home=malformed_overlay["home"],
            receipt_path=receipt_path,
            actor="operator",
            writer_quiesced=True,
        )["disposition"]
        == "rolled_back_and_hash_verified"
    )


def test_plan_rejects_symlink_and_hardlink_shards(
    malformed_overlay: dict[str, object], tmp_path: Path
) -> None:
    shard = malformed_overlay["shard"]
    original = malformed_overlay["original"]
    assert isinstance(shard, Path)
    assert isinstance(original, bytes)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(original)
    shard.unlink()
    shard.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe"):
        _plan(malformed_overlay)
    shard.unlink()
    os.link(outside, shard)
    with pytest.raises(ValueError, match="unsafe"):
        _plan(malformed_overlay)


def test_plan_rejects_a_schema_valid_target(malformed_overlay: dict[str, object]) -> None:
    original = malformed_overlay["original"]
    assert isinstance(original, bytes)
    first = original.splitlines(keepends=True)[0]
    with pytest.raises(ValueError, match="schema-valid"):
        plan_overlay_recovery(
            home=malformed_overlay["home"],
            writer="chiap08.jsonl",
            line_number=1,
            source_sha256=_sha(original),
            line_sha256=_sha(first),
            recovery_card_id="f0d0ba98",
            evidence=malformed_overlay["evidence"],
            actor="operator",
        )


def test_plan_refuses_to_leave_a_second_rejected_row(
    malformed_overlay: dict[str, object],
) -> None:
    shard = malformed_overlay["shard"]
    rejected = malformed_overlay["rejected"]
    assert isinstance(shard, Path)
    assert isinstance(rejected, bytes)
    shard.write_bytes(shard.read_bytes() + b'{"also":"wrong"}\n')
    raw = shard.read_bytes()
    with pytest.raises(ValueError, match="remains invalid"):
        plan_overlay_recovery(
            home=malformed_overlay["home"],
            writer="chiap08.jsonl",
            line_number=2,
            source_sha256=_sha(raw),
            line_sha256=_sha(rejected),
            recovery_card_id="f0d0ba98",
            evidence=malformed_overlay["evidence"],
            actor="operator",
        )


def test_scanner_reports_wrong_schema_with_card_hint(malformed_overlay: dict[str, object]) -> None:
    problems = scan_overlay_records(malformed_overlay["home"])
    assert len(problems) == 1
    assert problems[0]["file"] == "chiap08.jsonl"
    assert problems[0]["line"] == 2
    assert problems[0]["card_hint"] == "086ea05c"
    assert problems[0]["action_hint"] == "verdict"


def test_append_revalidates_existing_shard_under_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card as card_module

    monkeypatch.setattr(card_module.socket, "gethostname", lambda: "chiap08")
    shard = tmp_path / "coordination" / "card_events" / "chiap08.jsonl"
    shard.parent.mkdir(parents=True)
    shard.write_text('{"wrong":"schema"}\n', encoding="utf-8")
    before = shard.read_bytes()
    with pytest.raises(ValueError, match="existing overlay shard"):
        CardEventLog(tmp_path).append(
            CardEvent(card_id="healthy1", action="add_label", writer="operator", label="ok")
        )
    assert shard.read_bytes() == before


def test_append_validates_the_new_event_while_holding_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skcoord import card as card_module

    monkeypatch.setattr(card_module.socket, "gethostname", lambda: "chiap08")
    real_validate = card_module.validate_overlay_event

    def require_lock(event: CardEvent, *, require_writer: bool = True) -> None:
        if event.card_id == "healthy1":
            lock_path = tmp_path / "coordination" / "card_events" / ".chiap08.jsonl.lock"
            with lock_path.open("rb") as lock:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        real_validate(event, require_writer=require_writer)

    monkeypatch.setattr(card_module, "validate_overlay_event", require_lock)
    CardEventLog(tmp_path).append(
        CardEvent(card_id="healthy1", action="add_label", writer="operator", label="ok")
    )
