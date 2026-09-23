"""Exact stream recovery preserves evidence and keeps CardStore strict."""

import hashlib
import json
import os

import pytest

from skcoord import card_recovery as recovery
from skcoord import card_store
from skcoord.card_store import CardCore, CardStore


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def incident(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    store = CardStore(home)
    store.create(CardCore(id=recovery.CARD, title="Target", kind="task"))
    store.create(CardCore(id=recovery.RECOVERY_CARD, title="Recovery", kind="task",
                          initial_owner="operator", initial_claim_revision="revision"))
    first = {"event_id": recovery.EVENTS[0][0], "action": "claim", "seq": 0,
             "writer": "pi-seraph-38c4a706", "node": "chiap08", "prev_hash": "",
             "claim_revision": recovery.REVISION, "owner": "pi-seraph-38c4a706",
             "ts": "2026-09-21T00:00:00Z"}
    tail = [{"event_id": event_id, "action": action, "seq": index,
             "writer": first["writer"], "node": "chiap08", "prev_hash": "broken",
             "ts": f"2026-09-21T00:00:0{index}Z"}
            for index, (event_id, action) in enumerate(recovery.EVENTS[1:], 1)]
    prefix = (json.dumps(first) + "\n").encode()
    original = prefix + b"".join((json.dumps(event) + "\n").encode() for event in tail)
    writer = home / "cards" / recovery.CARD / "events" / recovery.WRITER
    writer.parent.mkdir(exist_ok=True)
    writer.write_bytes(original)
    monkeypatch.setattr(recovery, "ORIGINAL_SHA", digest(original))
    monkeypatch.setattr(recovery, "PREFIX_SHA", digest(prefix))
    monkeypatch.setattr(recovery, "CORE_SHA", digest((home / "cards" / recovery.CARD / "core.json").read_bytes()))
    monkeypatch.setattr(recovery, "load_legacy_mutations", lambda _home: {
        recovery.CARD: [{"action": "link", "link_key": "producer_identity"},
                        {"action": "link", "link_key": "guidance"}]})
    args = dict(home=home, card=recovery.CARD, writer=recovery.WRITER,
                source_sha=recovery.ORIGINAL_SHA, prefix_sha=recovery.PREFIX_SHA,
                recovery_card=recovery.RECOVERY_CARD, evidence=evidence, actor="operator")
    return store, writer, evidence, original, prefix, args


def test_exact_recovery_retry_append_and_evidence(incident, monkeypatch):
    store, writer, evidence, original, prefix, args = incident
    with pytest.raises(ValueError, match="chain broken"):
        store.fold(recovery.CARD)
    receipt = recovery.recover(**args)
    assert writer.read_bytes() == prefix
    assert receipt["disposition"] == "recovered_and_strictly_verified"
    assert (evidence / f"38c4a706-{recovery.ORIGINAL_SHA}.original.jsonl").read_bytes() == original
    assert recovery.recover(**args) == receipt
    monkeypatch.setattr(card_store, "_HOSTNAME", "chiap08")
    store.append_event(recovery.CARD, "note", "pi-seraph-38c4a706", body="normal append")
    assert len(writer.read_bytes().splitlines()) == 2
    assert store.fold(recovery.CARD) is not None


@pytest.mark.parametrize("field", ["card", "writer", "source_sha", "prefix_sha", "recovery_card"])
def test_wrong_pin_is_artifact_neutral(incident, field):
    _, writer, evidence, original, _, args = incident
    args[field] = "wrong"
    with pytest.raises(ValueError, match="mismatch"):
        recovery.recover(**args)
    assert writer.read_bytes() == original
    assert list(evidence.iterdir()) == []


def test_wrong_tail_and_concurrent_append_refused(incident):
    _, writer, evidence, original, _, args = incident
    writer.write_bytes(original + b'{}\n')
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        recovery.recover(**args)
    assert list(evidence.iterdir()) == []


def test_symlink_and_hardlink_writer_refused(incident, tmp_path):
    _, writer, evidence, original, _, args = incident
    other = tmp_path / "other"
    other.write_bytes(original)
    writer.unlink()
    writer.symlink_to(other)
    with pytest.raises(ValueError, match="unsafe"):
        recovery.recover(**args)
    writer.unlink()
    os.link(other, writer)
    with pytest.raises(ValueError, match="unsafe"):
        recovery.recover(**args)
    assert list(evidence.iterdir()) == []


def test_symlink_and_hardlink_evidence_refused(incident, tmp_path):
    _, writer, evidence, original, _, args = incident
    source_name = f"38c4a706-{recovery.ORIGINAL_SHA}.original.jsonl"
    other = tmp_path / "other"
    other.write_bytes(original)
    (evidence / source_name).symlink_to(other)
    with pytest.raises(ValueError, match="unsafe"):
        recovery.recover(**args)
    (evidence / source_name).unlink()
    os.link(other, evidence / source_name)
    with pytest.raises(ValueError, match="unsafe"):
        recovery.recover(**args)
    assert writer.read_bytes() == original


def test_interrupted_artifact_publication_retries(incident, monkeypatch):
    _, writer, evidence, original, prefix, args = incident
    real_link = recovery.os.link
    def interrupt(source, destination, **kwargs):
        if destination.endswith(".intent.json"):
            raise OSError("interrupted before intent publication")
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(recovery.os, "link", interrupt)
    with pytest.raises(OSError):
        recovery.recover(**args)
    assert writer.read_bytes() == original
    assert len(list(evidence.iterdir())) == 1
    monkeypatch.setattr(recovery.os, "link", real_link)
    recovery.recover(**args)
    assert writer.read_bytes() == prefix


def test_other_corrupt_writer_blocks_global_verification(incident, monkeypatch):
    store, writer, evidence, original, prefix, args = incident
    other = writer.parent / "another@chiap08.jsonl"
    original_verify = recovery._verify
    def corrupt_then_verify(current_store):
        other.write_bytes(b'{broken json}\n')
        original_verify(current_store)
    monkeypatch.setattr(recovery, "_verify", corrupt_then_verify)
    with pytest.raises(ValueError, match="malformed"):
        recovery.recover(**args)
    assert writer.read_bytes() == prefix
    assert not list(evidence.glob("*.receipt.json"))
    monkeypatch.setattr(recovery, "_verify", original_verify)
    with pytest.raises(ValueError, match="malformed"):
        recovery.recover(**args)


def test_conflicting_legacy_event_refused_before_replacement(incident, monkeypatch):
    _, writer, evidence, original, _, args = incident
    monkeypatch.setattr(recovery, "load_legacy_mutations", lambda _home: {
        recovery.CARD: [{"action": "verdict", "link_key": "verdict", "link_value": "PASS"}]})
    with pytest.raises(ValueError, match="conflicting legacy"):
        recovery.recover(**args)
    assert writer.read_bytes() == original
    assert list(evidence.iterdir()) == []


def test_legacy_change_between_precheck_and_verification_refused(incident, monkeypatch):
    _, writer, evidence, _, prefix, args = incident
    calls = 0
    def changing_legacy(_home):
        nonlocal calls
        calls += 1
        events = [{"action": "link", "link_key": "producer_identity"},
                  {"action": "link", "link_key": "guidance"}]
        if calls > 1:
            events.append({"action": "complete"})
        return {recovery.CARD: events}
    monkeypatch.setattr(recovery, "load_legacy_mutations", changing_legacy)
    with pytest.raises(ValueError, match="conflicting legacy"):
        recovery.recover(**args)
    assert writer.read_bytes() == prefix
    assert not list(evidence.glob("*.receipt.json"))


def test_rollback_refuses_other_writer_append(incident):
    _, writer, evidence, _, _, args = incident
    recovery.recover(**args)
    other = writer.parent / "other@chiap08.jsonl"
    other.write_bytes(b'{"event_id":"later","action":"note"}\n')
    rollback_args = dict(home=args["home"], card=args["card"], writer=args["writer"],
                         post_sha=args["prefix_sha"], recovery_card=args["recovery_card"],
                         evidence=evidence, actor="operator")
    with pytest.raises(ValueError, match="intervening writer change"):
        recovery.rollback(**rollback_args)


def test_interrupted_after_intent_retries(incident, monkeypatch):
    _, writer, evidence, original, prefix, args = incident
    actual = recovery._replace
    def interrupt(*items):
        actual(*items)
        raise OSError("power loss after replacement")
    monkeypatch.setattr(recovery, "_replace", interrupt)
    with pytest.raises(OSError):
        recovery.recover(**args)
    assert writer.read_bytes() == prefix
    assert len(list(evidence.iterdir())) == 2
    monkeypatch.setattr(recovery, "_replace", actual)
    assert recovery.recover(**args)["disposition"] == "recovered_and_strictly_verified"


def test_rollback_and_intervening_append_refusal(incident):
    store, writer, evidence, original, prefix, args = incident
    recovery.recover(**args)
    rollback_args = dict(home=args["home"], card=args["card"], writer=args["writer"],
                         post_sha=args["prefix_sha"], recovery_card=args["recovery_card"],
                         evidence=evidence, actor="operator")
    assert recovery.rollback(**rollback_args)["restored_sha256"] == digest(original)
    assert writer.read_bytes() == original
    writer.write_bytes(prefix + b'{"action":"note"}\n')
    with pytest.raises(ValueError, match="intervening"):
        recovery.rollback(**rollback_args)
