"""Exact, evidence-preserving recovery for the 38c4a706 writer stream.

Invoke with ``python -m skcoord.card_recovery recover|rollback``. This module
does not relax the normal CardStore reader or append protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .card_store import CardStore, card_mutation_lock, load_legacy_mutations

CARD = "38c4a706"
WRITER = "pi-seraph-38c4a706@chiap08.jsonl"
RECOVERY_CARD = "7d38c4a7"
ORIGINAL_SHA = "07fcd8ff2040401a437290fc0575812ae0f50174fcce4104e12aa4a499303600"
PREFIX_SHA = "159e7fa6330ac3a6f30f9d559d88b018b30080cc1471028aa22c981825033977"
CORE_SHA = "be18eb005967653fdc5f71c423cab8daa1d8ffa6551c851c6084e6d657713276"
EVENTS = (
    ("3e9f8c999df64df1b7a0166e888bfc57", "claim"),
    ("6b86b273ff34fce19d6b804eff5a3f57", "link"),
    ("d4735e3a265e16eee03f59718b9b5d03", "verdict"),
    ("4e07408562bedb8b60ce05c1decfe3ad", "complete"),
)
REVISION = "783d389dc9874170ba9260790bb87b2b"
TOOL_REVISION = "skcoord-38c4-recovery-1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _directory(path: Path) -> int:
    """Open an existing absolute directory without following any component."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("evidence destination must be an absolute existing directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, name: str) -> bytes | None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"unsafe file: {name}")
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        opened = os.fstat(handle)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError(f"file changed while opening: {name}")
        with os.fdopen(handle, "rb", closefd=False) as stream:
            data = stream.read()
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
            raise ValueError(f"file changed while reading: {name}")
        return data
    finally:
        os.close(handle)


def _create(fd: int, name: str, data: bytes) -> None:
    temp = f".{name}.{uuid.uuid4().hex}.tmp"
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fsync(handle)
    finally:
        os.close(handle)
    try:
        os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)


def _replace(fd: int, name: str, data: bytes, expected: str) -> None:
    if _sha(_read(fd, name) or b"") != expected:
        raise ValueError("writer changed before replacement")
    temp = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        _create(fd, temp, data)
        if _sha(_read(fd, name) or b"") != expected:
            raise ValueError("writer changed before replacement")
        os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temp, dir_fd=fd)
        except FileNotFoundError:
            pass


def _manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()


def _check_intent(intent: dict, *, actor: str, source: Path, destination: Path) -> None:
    expected = {"card_id": CARD, "recovery_card_id": RECOVERY_CARD,
                "actor": actor, "source_sha256": ORIGINAL_SHA, "retained_sha256": PREFIX_SHA,
                "core_sha256": CORE_SHA, "event_ids": [item[0] for item in EVENTS],
                "source": str(source), "destination": str(destination),
                "tool_revision": TOOL_REVISION, "disposition": "intent_to_retain_valid_claim"}
    if not isinstance(intent, dict) or any(intent.get(key) != value for key, value in expected.items()):
        raise ValueError("recovery intent does not match exact transaction")


def _check_source(store: CardStore, events_fd: int, raw: bytes) -> bytes:
    if _sha(raw) != ORIGINAL_SHA:
        raise ValueError("original writer SHA256 mismatch")
    core_fd = store._open_existing_card_directory(CARD)
    if core_fd is None:
        raise ValueError("target card directory missing")
    try:
        core = store._read_regular_file_bytes(core_fd, "core.json", "recovery core")
    finally:
        os.close(core_fd)
    if core is None or _sha(core) != CORE_SHA or json.loads(core).get("id") != CARD:
        raise ValueError("target core identity or SHA256 mismatch")
    lines = raw.splitlines(keepends=True)
    if len(lines) != 4 or any(not line.endswith(b"\n") for line in lines):
        raise ValueError("expected exactly four complete event lines")
    prefix = lines[0]
    if _sha(prefix) != PREFIX_SHA:
        raise ValueError("valid prefix SHA256 mismatch")
    decoded = [json.loads(line) for line in lines]
    if any(not isinstance(event, dict) for event in decoded):
        raise ValueError("writer must contain JSON event objects")
    for index, (event, (event_id, action)) in enumerate(zip(decoded, EVENTS)):
        if event.get("event_id") != event_id or event.get("action") != action or event.get("seq") != index:
            raise ValueError("unexpected event identity, action, or order")
        if event.get("writer") != "pi-seraph-38c4a706" or event.get("node") != "chiap08":
            raise ValueError("unexpected writer identity")
    first = decoded[0]
    if (first.get("prev_hash") != "" or first.get("claim_revision") != REVISION
            or first.get("owner") != "pi-seraph-38c4a706"):
        raise ValueError("first event is not the pinned valid claim")
    if decoded[1].get("prev_hash") == _sha(lines[0].strip()):
        raise ValueError("selected tail does not have the incident chain break")
    return prefix


def _open_events(store: CardStore) -> int:
    card_fd = store._open_existing_card_directory(CARD)
    if card_fd is None:
        raise ValueError("target card directory missing")
    try:
        events_fd = store._open_existing_directory(card_fd, "events", "recovery events")
    finally:
        os.close(card_fd)
    if events_fd is None:
        raise ValueError("target events directory missing")
    return events_fd


def _verify(store: CardStore) -> None:
    _check_legacy(store)
    if store.fold(CARD) is None:
        raise ValueError("target fold missing")
    store.list_cards(include_archived=True)


def _check_legacy(store: CardStore) -> None:
    """Permit only the two known provenance links in sanctioned legacy lanes."""
    events = load_legacy_mutations(store.home).get(CARD, [])
    keys = []
    for event in events:
        if (event.get("action") != "link" or event.get("link_key") not in
                {"producer_identity", "guidance"}):
            raise ValueError("conflicting legacy event on target card")
        keys.append(event["link_key"])
    if sorted(keys) != ["guidance", "producer_identity"]:
        raise ValueError("unexpected legacy event set on target card")


def _event_snapshot(events_fd: int) -> dict[str, str]:
    snapshot = {}
    for name in sorted(os.listdir(events_fd)):
        if not name.endswith(".jsonl"):
            continue
        raw = _read(events_fd, name)
        if raw is None:
            raise ValueError("writer vanished during snapshot")
        snapshot[name] = _sha(raw)
    return snapshot


def _check_evidence_location(home: Path, evidence: Path) -> None:
    if not evidence.is_absolute():
        raise ValueError("evidence destination must be absolute")
    try:
        within_home = evidence.relative_to(home)
    except ValueError:
        return
    if not within_home.parts or within_home.parts[0] != "evidence":
        raise ValueError("evidence inside CardStore home must use evidence directory")


def recover(*, home: Path, card: str, writer: str, source_sha: str, prefix_sha: str,
            recovery_card: str, evidence: Path, actor: str) -> dict:
    if (card, writer, source_sha, prefix_sha, recovery_card) != (CARD, WRITER, ORIGINAL_SHA, PREFIX_SHA, RECOVERY_CARD):
        raise ValueError("recovery target or digest mismatch")
    store = CardStore(home)
    evidence = Path(evidence)
    _check_evidence_location(store.home, evidence)
    with card_mutation_lock(store.home, CARD, artifact_neutral=True):
        authority = store.fold(RECOVERY_CARD)
        if authority is None or authority.owner != actor or authority.status.value != "doing":
            raise ValueError("recovery card must be doing and claimed by actor")
        events_fd = _open_events(store)
        evidence_fd = _directory(evidence)
        try:
            _check_legacy(store)
            raw = _read(events_fd, WRITER)
            if raw is None:
                raise ValueError("target writer missing")
            stem = f"38c4a706-{ORIGINAL_SHA}"
            source_name, intent_name, receipt_name = stem + ".original.jsonl", stem + ".intent.json", stem + ".receipt.json"
            source_path = store.cards_dir / CARD / "events" / WRITER
            evidence_path = evidence / source_name
            intent_raw = _read(evidence_fd, intent_name)
            original_evidence = _read(evidence_fd, source_name)
            if intent_raw is not None:
                intent = json.loads(intent_raw)
                _check_intent(intent, actor=actor, source=source_path, destination=evidence_path)
                if (intent.get("source_sha256") != ORIGINAL_SHA or intent.get("retained_sha256") != PREFIX_SHA
                        or original_evidence is None or _sha(original_evidence) != ORIGINAL_SHA):
                    raise ValueError("recovery evidence or intent mismatch")
                if raw != original_evidence and _sha(raw) != PREFIX_SHA:
                    raise ValueError("unexpected writer state after recovery intent")
            else:
                prefix = _check_source(store, events_fd, raw)
                if original_evidence is not None and original_evidence != raw:
                    raise ValueError("existing evidence differs from source")
                if original_evidence is None:
                    _create(evidence_fd, source_name, raw)
                intent = {"card_id": CARD, "recovery_card_id": RECOVERY_CARD,
                          "actor": actor, "source_sha256": ORIGINAL_SHA, "retained_sha256": PREFIX_SHA,
                          "core_sha256": CORE_SHA, "event_ids": [item[0] for item in EVENTS],
                          "source": str(source_path),
                          "destination": str(evidence_path), "tool_revision": TOOL_REVISION,
                          "time": datetime.now(timezone.utc).isoformat(), "disposition": "intent_to_retain_valid_claim"}
                _create(evidence_fd, intent_name, _manifest_bytes(intent))
            receipt_raw = _read(evidence_fd, receipt_name)
            if receipt_raw is not None:
                receipt = json.loads(receipt_raw)
                if (_sha(raw) != PREFIX_SHA or receipt.get("retained_sha256") != PREFIX_SHA
                        or receipt.get("disposition") != "recovered_and_strictly_verified"
                        or receipt.get("writer_snapshot") != _event_snapshot(events_fd)
                        or any(receipt.get(key) != value for key, value in intent.items()
                               if key != "disposition")):
                    raise ValueError("completed recovery conflicts with current writer")
                return receipt
            if _sha(raw) == ORIGINAL_SHA:
                prefix = _check_source(store, events_fd, raw)
                _replace(events_fd, WRITER, prefix, ORIGINAL_SHA)
            _verify(CardStore(store.home))
            receipt = dict(intent)
            receipt["disposition"] = "recovered_and_strictly_verified"
            receipt["verified_at"] = datetime.now(timezone.utc).isoformat()
            receipt["writer_snapshot"] = _event_snapshot(events_fd)
            _create(evidence_fd, receipt_name, _manifest_bytes(receipt))
            return receipt
        finally:
            os.close(evidence_fd)
            os.close(events_fd)


def rollback(*, home: Path, card: str, writer: str, post_sha: str, recovery_card: str,
             evidence: Path, actor: str) -> dict:
    if (card, writer, post_sha, recovery_card) != (CARD, WRITER, PREFIX_SHA, RECOVERY_CARD):
        raise ValueError("rollback target or digest mismatch")
    store = CardStore(home)
    evidence = Path(evidence)
    _check_evidence_location(store.home, evidence)
    with card_mutation_lock(store.home, CARD, artifact_neutral=True):
        authority = store.fold(RECOVERY_CARD)
        if authority is None or authority.owner != actor or authority.status.value != "doing":
            raise ValueError("recovery card must be doing and claimed by actor")
        events_fd = _open_events(store)
        evidence_fd = _directory(evidence)
        try:
            stem = f"38c4a706-{ORIGINAL_SHA}"
            intent_raw = _read(evidence_fd, stem + ".intent.json")
            original = _read(evidence_fd, stem + ".original.jsonl")
            if intent_raw is None or original is None or _sha(original) != ORIGINAL_SHA:
                raise ValueError("complete original evidence and intent required")
            _check_intent(json.loads(intent_raw), actor=actor,
                          source=store.cards_dir / CARD / "events" / WRITER,
                          destination=evidence / (stem + ".original.jsonl"))
            completed_raw = _read(evidence_fd, stem + ".receipt.json")
            if completed_raw is None:
                raise ValueError("completed recovery receipt required for rollback")
            completed = json.loads(completed_raw)
            baseline = completed.get("writer_snapshot")
            if (not isinstance(baseline, dict) or baseline.get(WRITER) != PREFIX_SHA
                    or completed.get("disposition") != "recovered_and_strictly_verified"):
                raise ValueError("completed writer snapshot missing or invalid")
            rollback_intent_name = stem + ".rollback-intent.json"
            rollback_receipt_name = stem + ".rollback.json"
            rollback_intent_raw = _read(evidence_fd, rollback_intent_name)
            current_sha = _sha(_read(events_fd, WRITER) or b"")
            current_snapshot = _event_snapshot(events_fd)
            expected_snapshot = dict(baseline)
            if rollback_intent_raw is not None and current_sha == ORIGINAL_SHA:
                expected_snapshot[WRITER] = ORIGINAL_SHA
            if current_snapshot != expected_snapshot:
                raise ValueError("rollback refused: intervening writer change")
            if rollback_intent_raw is None:
                if current_sha != PREFIX_SHA:
                    raise ValueError("rollback refused: intervening writer change")
                rollback_intent = {"card_id": CARD, "recovery_card_id": RECOVERY_CARD,
                                   "actor": actor, "source_sha256": PREFIX_SHA,
                                   "restored_sha256": ORIGINAL_SHA,
                                   "time": datetime.now(timezone.utc).isoformat(),
                                   "tool_revision": TOOL_REVISION,
                                   "disposition": "intent_to_restore_original"}
                _create(evidence_fd, rollback_intent_name, _manifest_bytes(rollback_intent))
            else:
                rollback_intent = json.loads(rollback_intent_raw)
                if (rollback_intent.get("actor") != actor
                        or rollback_intent.get("source_sha256") != PREFIX_SHA
                        or rollback_intent.get("restored_sha256") != ORIGINAL_SHA
                        or rollback_intent.get("disposition") != "intent_to_restore_original"):
                    raise ValueError("rollback intent mismatch")
                if current_sha not in (PREFIX_SHA, ORIGINAL_SHA):
                    raise ValueError("rollback refused: intervening writer change")
            receipt_raw = _read(evidence_fd, rollback_receipt_name)
            if receipt_raw is not None:
                receipt = json.loads(receipt_raw)
                if current_sha != ORIGINAL_SHA or receipt.get("restored_sha256") != ORIGINAL_SHA:
                    raise ValueError("rollback receipt conflicts with writer")
                return receipt
            if current_sha == PREFIX_SHA:
                _replace(events_fd, WRITER, original, PREFIX_SHA)
            receipt = {"card_id": CARD, "recovery_card_id": RECOVERY_CARD, "actor": actor,
                       "source_sha256": PREFIX_SHA, "restored_sha256": ORIGINAL_SHA,
                       "time": datetime.now(timezone.utc).isoformat(), "tool_revision": TOOL_REVISION,
                       "disposition": "original_stream_restored"}
            _create(evidence_fd, rollback_receipt_name, _manifest_bytes(receipt))
            return receipt
        finally:
            os.close(evidence_fd)
            os.close(events_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("recover", "rollback"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--card", required=True)
    parser.add_argument("--writer", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--prefix-sha", required=True)
    parser.add_argument("--recovery-card", required=True)
    parser.add_argument("--evidence-dir", dest="evidence", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = vars(parser.parse_args(argv))
    operation = args.pop("operation")
    if operation == "rollback":
        args["post_sha"] = args.pop("prefix_sha")
        if args.pop("source_sha") != ORIGINAL_SHA:
            parser.error("source SHA256 mismatch")
        result = rollback(**args)
    else:
        result = recover(**args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
