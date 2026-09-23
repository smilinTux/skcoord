"""Hash-pinned recovery for one rejected coordination overlay record."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .card import CardEvent, CardEventLog, validate_overlay_event
from .card_store import CardStore

PLAN_SCHEMA = "skcoord.overlay-recovery-plan.v1"
PLAN_SCHEMA_V2 = "skcoord.overlay-recovery-plan.v2"
RECEIPT_SCHEMA = "skcoord.overlay-recovery-receipt.v1"
RECEIPT_SCHEMA_V2 = "skcoord.overlay-recovery-receipt.v2"
ROLLBACK_SCHEMA = "skcoord.overlay-recovery-rollback.v1"
TOOL_REVISION = "f0d0ba98-overlay-recovery-1"
TOOL_REVISION_V2 = "a62b5f8d-overlay-recovery-2"
_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
_ACTOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}\Z")
_PLAN_FIELDS = {
    "schema",
    "tool_revision",
    "writer",
    "source",
    "line_number",
    "source_sha256",
    "line_sha256",
    "repaired_sha256",
    "recovery_card_id",
    "evidence",
    "actor",
    "diagnostic",
    "requires_writer_quiescence",
    "planned_at",
    "plan_sha256",
}
_PLAN_FIELDS_V2 = {
    "schema",
    "tool_revision",
    "writer",
    "source",
    "targets",
    "source_sha256",
    "repaired_sha256",
    "recovery_card_id",
    "evidence",
    "actor",
    "requires_writer_quiescence",
    "planned_at",
    "plan_sha256",
}
_RECEIPT_FIELDS = {
    "schema",
    "disposition",
    "plan_sha256",
    "writer",
    "source",
    "source_sha256",
    "line_sha256",
    "repaired_sha256",
    "recovery_card_id",
    "actor",
    "evidence",
    "plan_artifact",
    "original_artifact",
    "rejected_artifact",
    "intent_artifact",
    "receipt_artifact",
    "verified_at",
    "receipt_sha256",
}
_RECEIPT_FIELDS_V2 = {
    "schema",
    "disposition",
    "plan_sha256",
    "writer",
    "source",
    "targets",
    "source_sha256",
    "repaired_sha256",
    "recovery_card_id",
    "actor",
    "evidence",
    "plan_artifact",
    "original_artifact",
    "rejected_artifacts",
    "intent_artifact",
    "receipt_artifact",
    "verified_at",
    "receipt_sha256",
}
_TARGET_FIELDS = {"line_number", "line_sha256", "diagnostic"}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()


def _seal(payload: dict[str, Any], field: str) -> dict[str, Any]:
    sealed = dict(payload)
    sealed[field] = _sha(_json_bytes(sealed))
    return sealed


def _verify_seal(payload: dict[str, Any], field: str) -> None:
    expected = payload.get(field)
    body = dict(payload)
    body.pop(field, None)
    if not isinstance(expected, str) or _sha(_json_bytes(body)) != expected:
        raise ValueError(f"{field} mismatch")


def _validate_targets(targets: Any) -> None:
    """Validate an ordered nonempty list of independently pinned rows."""
    if not isinstance(targets, list) or not targets:
        raise ValueError("recovery targets must be a nonempty list")
    previous = 0
    for target in targets:
        if not isinstance(target, dict) or set(target) != _TARGET_FIELDS:
            raise ValueError("recovery target fields mismatch")
        line_number = target["line_number"]
        if (
            not isinstance(line_number, int)
            or isinstance(line_number, bool)
            or line_number <= previous
        ):
            raise ValueError("recovery target lines must be strictly increasing")
        if not isinstance(target["line_sha256"], str) or not _SHA_RE.fullmatch(
            target["line_sha256"]
        ):
            raise ValueError("recovery target SHA256 is invalid")
        diagnostic = target["diagnostic"]
        if (
            not isinstance(diagnostic, dict)
            or diagnostic.get("line") != line_number
            or diagnostic.get("line_sha256") != target["line_sha256"]
        ):
            raise ValueError("recovery target diagnostic is invalid")
        previous = line_number


def _validate_plan(plan: dict[str, Any]) -> None:
    """Reject recovery plans that drift from a sealed supported schema."""
    schema = plan.get("schema")
    expected_fields = _PLAN_FIELDS_V2 if schema == PLAN_SCHEMA_V2 else _PLAN_FIELDS
    if set(plan) != expected_fields:
        raise ValueError("recovery plan fields mismatch")
    _verify_seal(plan, "plan_sha256")
    expected_revision = TOOL_REVISION_V2 if schema == PLAN_SCHEMA_V2 else TOOL_REVISION
    if schema not in {PLAN_SCHEMA, PLAN_SCHEMA_V2} or (
        plan["tool_revision"] != expected_revision
        or plan["requires_writer_quiescence"] is not True
    ):
        raise ValueError("recovery plan schema mismatch")
    if not isinstance(plan["writer"], str) or not isinstance(plan["actor"], str):
        raise ValueError("recovery plan identity fields are invalid")
    _validate_writer(plan["writer"])
    _validate_actor(plan["actor"])
    sha_fields = ["source_sha256", "repaired_sha256", "plan_sha256"]
    if schema == PLAN_SCHEMA:
        sha_fields.append("line_sha256")
    if any(
        not isinstance(plan[key], str) or not _SHA_RE.fullmatch(plan[key]) for key in sha_fields
    ):
        raise ValueError("recovery plan SHA256 fields are invalid")
    if schema == PLAN_SCHEMA_V2:
        _validate_targets(plan["targets"])
    elif (
        not isinstance(plan["line_number"], int)
        or isinstance(plan["line_number"], bool)
        or plan["line_number"] < 1
        or not isinstance(plan["diagnostic"], dict)
    ):
        raise ValueError("recovery plan line or diagnostic is invalid")


def _validate_receipt(receipt: Any) -> None:
    """Reject recovery receipts that drift from a sealed supported schema."""
    if not isinstance(receipt, dict):
        raise ValueError("recovery receipt fields mismatch")
    schema = receipt.get("schema")
    expected_fields = _RECEIPT_FIELDS_V2 if schema == RECEIPT_SCHEMA_V2 else _RECEIPT_FIELDS
    if set(receipt) != expected_fields:
        raise ValueError("recovery receipt fields mismatch")
    _verify_seal(receipt, "receipt_sha256")
    if (
        schema not in {RECEIPT_SCHEMA, RECEIPT_SCHEMA_V2}
        or receipt["disposition"] != "applied_and_schema_verified"
    ):
        raise ValueError("recovery receipt schema mismatch")
    if schema == RECEIPT_SCHEMA_V2:
        _validate_targets(receipt["targets"])
        artifacts = receipt["rejected_artifacts"]
        if (
            not isinstance(artifacts, list)
            or len(artifacts) != len(receipt["targets"])
            or any(
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\\" in name
                or ".." in name
                for name in artifacts
            )
        ):
            raise ValueError("recovery rejected artifacts are invalid")
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("recovery rejected artifacts are invalid")


def _open_directory(path: Path) -> int:
    """Open an existing absolute directory without following any component."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("evidence destination must be an absolute existing directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(directory_fd: int, name: str) -> bytes | None:
    """Read one regular, single-link file through its pinned parent."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("recovery artifact name is unsafe")
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"unsafe file: {name}")
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"unsafe file: {name}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise ValueError(f"file changed while reading: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _publish(directory_fd: int, name: str, raw: bytes) -> None:
    """Create an immutable artifact, accepting only an exact retry."""
    existing = _read(directory_fd, name)
    if existing is not None:
        if existing != raw:
            raise ValueError(f"existing recovery artifact conflicts: {name}")
        return
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.fsync(directory_fd)
    finally:
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)


def _replace(directory_fd: int, name: str, raw: bytes, expected_sha256: str) -> None:
    """Atomically replace one locked shard after a final hash check."""
    if _sha(_read(directory_fd, name) or b"") != expected_sha256:
        raise ValueError("overlay source SHA256 changed before replacement")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        if _sha(_read(directory_fd, name) or b"") != expected_sha256:
            raise ValueError("overlay source SHA256 changed before replacement")
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def diagnose_overlay_line(raw: bytes, *, file: str, line: int) -> dict[str, Any] | None:
    """Return a stable schema diagnostic for one physical overlay line."""
    parsed: Any = None
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text)
        event = CardEvent.model_validate(parsed)
        validate_overlay_event(event)
        return None
    except Exception as exc:  # noqa: BLE001
        card_hint = None
        action_hint = None
        if isinstance(parsed, dict):
            card_hint = parsed.get("card_id") or parsed.get("card")
            action_hint = parsed.get("action") or parsed.get("event")
        return {
            "file": file,
            "line": line,
            "line_sha256": _sha(raw),
            "card_hint": str(card_hint) if card_hint is not None else None,
            "action_hint": str(action_hint) if action_hint is not None else None,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _lines(raw: bytes) -> list[bytes]:
    return raw.splitlines(keepends=True)


def _validate_clean_shard(raw: bytes, writer: str) -> None:
    for number, line in enumerate(_lines(raw), start=1):
        if not line.strip():
            continue
        diagnostic = diagnose_overlay_line(line, file=writer, line=number)
        if diagnostic is not None:
            raise ValueError(
                f"repaired overlay shard remains invalid at line {number}: "
                f"{diagnostic['error_type']}"
            )


def scan_overlay_records(home: Path) -> list[dict[str, Any]]:
    """Schema-validate all overlay rows without changing the store."""
    log = CardEventLog(Path(home))
    directory_fd = log._open_existing_event_directory()
    if directory_fd is None:
        return []
    rejected: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(directory_fd)):
            if not name.endswith(".jsonl"):
                continue
            raw = log._read_regular_file_bytes(directory_fd, name)
            if raw is None:
                continue
            source_sha = _sha(raw)
            for number, line in enumerate(_lines(raw), start=1):
                if not line.strip():
                    continue
                diagnostic = diagnose_overlay_line(line, file=name, line=number)
                if diagnostic is not None:
                    diagnostic["source_sha256"] = source_sha
                    rejected.append(diagnostic)
    finally:
        os.close(directory_fd)
    return rejected


def _validate_writer(writer: str) -> None:
    if (
        not writer.endswith(".jsonl")
        or not writer
        or "/" in writer
        or "\\" in writer
        or ".." in writer
    ):
        raise ValueError("overlay writer filename is unsafe")


def _validate_actor(actor: str) -> None:
    if not _ACTOR_RE.fullmatch(actor):
        raise ValueError("recovery actor must be a nonempty bounded identity")


def _evidence_directory(home: Path, evidence: Path) -> int:
    evidence = Path(evidence)
    if not evidence.is_absolute():
        raise ValueError("evidence destination must be absolute")
    try:
        relative = evidence.relative_to(Path(home).expanduser())
    except ValueError:
        pass
    else:
        if not relative.parts or relative.parts[0] != "evidence":
            raise ValueError("evidence inside coordination home must use evidence directory")
    return _open_directory(evidence)


def _requested_targets(
    line_number: int | Sequence[int], line_sha256: str | Sequence[str]
) -> list[tuple[int, str]]:
    """Normalize one or more CLI/API line and digest pairs."""
    numbers = (line_number,) if isinstance(line_number, int) else tuple(line_number)
    hashes = (line_sha256,) if isinstance(line_sha256, str) else tuple(line_sha256)
    if not numbers or len(numbers) != len(hashes):
        raise ValueError("recovery line and SHA256 counts must match and be nonempty")
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 1 for number in numbers
    ):
        raise ValueError("line number must be positive")
    if tuple(sorted(set(numbers))) != numbers:
        raise ValueError("recovery line numbers must be unique and strictly increasing")
    if any(not isinstance(value, str) or not _SHA_RE.fullmatch(value) for value in hashes):
        raise ValueError("source and line SHA256 values must be lowercase hex")
    return list(zip(numbers, hashes, strict=True))


def _repair_targets(
    lines: list[bytes], writer: str, requested: Sequence[tuple[int, str]]
) -> tuple[list[dict[str, Any]], list[bytes], bytes]:
    """Revalidate targets against one source image and build one repaired image."""
    targets: list[dict[str, Any]] = []
    rejected_rows: list[bytes] = []
    target_numbers: set[int] = set()
    for line_number, line_sha256 in requested:
        if line_number > len(lines):
            raise ValueError("overlay line number is out of range")
        rejected = lines[line_number - 1]
        if _sha(rejected) != line_sha256:
            raise ValueError("overlay line SHA256 mismatch")
        diagnostic = diagnose_overlay_line(rejected, file=writer, line=line_number)
        if diagnostic is None:
            raise ValueError("selected overlay row is schema-valid")
        targets.append(
            {
                "line_number": line_number,
                "line_sha256": line_sha256,
                "diagnostic": diagnostic,
            }
        )
        rejected_rows.append(rejected)
        target_numbers.add(line_number)
    repaired = b"".join(
        line for number, line in enumerate(lines, start=1) if number not in target_numbers
    )
    _validate_clean_shard(repaired, writer)
    return targets, rejected_rows, repaired


def _targets_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the common target representation for v1 and v2 plans."""
    if plan["schema"] == PLAN_SCHEMA_V2:
        return list(plan["targets"])
    return [
        {
            "line_number": plan["line_number"],
            "line_sha256": plan["line_sha256"],
            "diagnostic": plan["diagnostic"],
        }
    ]


def plan_overlay_recovery(
    *,
    home: Path,
    writer: str,
    line_number: int | Sequence[int],
    source_sha256: str,
    line_sha256: str | Sequence[str],
    recovery_card_id: str,
    evidence: Path,
    actor: str,
) -> dict[str, Any]:
    """Build a hash-bound plan for one or more rejected overlay rows."""
    home = Path(home).expanduser()
    _validate_writer(writer)
    _validate_actor(actor)
    if not _SHA_RE.fullmatch(source_sha256):
        raise ValueError("source and line SHA256 values must be lowercase hex")
    requested = _requested_targets(line_number, line_sha256)
    evidence_fd = _evidence_directory(home, Path(evidence))
    os.close(evidence_fd)

    log = CardEventLog(home)
    directory_fd = log._open_existing_event_directory()
    if directory_fd is None:
        raise ValueError("overlay event directory is absent")
    try:
        raw = log._read_regular_file_bytes(directory_fd, writer)
    finally:
        os.close(directory_fd)
    if raw is None:
        raise ValueError("overlay writer is absent")
    if _sha(raw) != source_sha256:
        raise ValueError("overlay source SHA256 mismatch")
    lines = _lines(raw)
    targets, _, repaired = _repair_targets(lines, writer, requested)
    source = (home / "coordination" / "card_events" / writer).resolve(strict=True)
    common = {
        "writer": writer,
        "source": str(source),
        "source_sha256": source_sha256,
        "repaired_sha256": _sha(repaired),
        "recovery_card_id": recovery_card_id,
        "evidence": str(Path(evidence)),
        "actor": actor,
        "requires_writer_quiescence": True,
        "planned_at": _now(),
    }
    if len(targets) == 1:
        target = targets[0]
        plan = {
            "schema": PLAN_SCHEMA,
            "tool_revision": TOOL_REVISION,
            **common,
            "line_number": target["line_number"],
            "line_sha256": target["line_sha256"],
            "diagnostic": target["diagnostic"],
        }
    else:
        plan = {
            "schema": PLAN_SCHEMA_V2,
            "tool_revision": TOOL_REVISION_V2,
            **common,
            "targets": targets,
        }
    return _seal(plan, "plan_sha256")


def save_recovery_plan(plan: dict[str, Any]) -> Path:
    """Persist a validated plan in its bound evidence directory."""
    _validate_plan(plan)
    evidence = Path(str(plan.get("evidence", "")))
    directory_fd = _open_directory(evidence)
    name = f"overlay-{plan['plan_sha256']}.plan.json"
    try:
        _publish(directory_fd, name, _json_bytes(plan))
    finally:
        os.close(directory_fd)
    return evidence / name


def _load_artifact(path: Path) -> dict[str, Any]:
    path = Path(path)
    directory_fd = _open_directory(path.parent)
    try:
        raw = _read(directory_fd, path.name)
    finally:
        os.close(directory_fd)
    if raw is None:
        raise ValueError(f"recovery artifact is absent: {path.name}")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"recovery artifact is malformed: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"recovery artifact must be an object: {path.name}")
    return payload


def _require_authority(home: Path, card_id: str, actor: str) -> None:
    card = CardStore(home).fold(card_id)
    if card is None or card.owner != actor or card.status.value != "doing":
        raise ValueError("recovery card must be doing and claimed by actor")


def apply_overlay_recovery(
    *, home: Path, plan_path: Path, actor: str, writer_quiesced: bool = False
) -> dict[str, Any]:
    """Apply one exact recovery plan and return its durable receipt."""
    plan = _load_artifact(Path(plan_path))
    _validate_plan(plan)
    if plan.get("schema") not in {PLAN_SCHEMA, PLAN_SCHEMA_V2} or plan.get("actor") != actor:
        raise ValueError("recovery plan identity mismatch")
    if not writer_quiesced:
        raise ValueError("overlay writers must be upgraded or quiesced before apply")
    _validate_actor(actor)
    home = Path(home).expanduser()
    source = (home / "coordination" / "card_events" / str(plan["writer"])).resolve(strict=True)
    if plan.get("source") != str(source):
        raise ValueError("recovery plan target path mismatch")
    _require_authority(home, str(plan["recovery_card_id"]), actor)
    evidence = Path(str(plan["evidence"]))
    evidence_fd = _evidence_directory(home, evidence)
    stem = f"overlay-{plan['plan_sha256']}"
    original_name = f"{stem}.original.jsonl"
    targets = _targets_from_plan(plan)
    requested = [(int(target["line_number"]), str(target["line_sha256"])) for target in targets]
    multi = plan["schema"] == PLAN_SCHEMA_V2
    rejected_names = (
        [f"{stem}.rejected-line-{target['line_number']}.bin" for target in targets]
        if multi
        else [f"{stem}.rejected.bin"]
    )
    intent_name = f"{stem}.intent.json"
    receipt_name = f"{stem}.receipt.json"
    log = CardEventLog(home)
    try:
        with log.writer_lock(str(plan["writer"])) as directory_fd:
            raw = log._read_regular_file_bytes(directory_fd, str(plan["writer"]))
            if raw is None:
                raise ValueError("overlay writer is absent")
            current_sha = _sha(raw)
            receipt_raw = _read(evidence_fd, receipt_name)
            if receipt_raw is not None:
                receipt = json.loads(receipt_raw)
                _validate_receipt(receipt)
                expected_receipt_schema = RECEIPT_SCHEMA_V2 if multi else RECEIPT_SCHEMA
                if (
                    receipt["schema"] != expected_receipt_schema
                    or receipt["plan_sha256"] != plan["plan_sha256"]
                ):
                    raise ValueError("completed recovery receipt does not match plan")
                if current_sha != plan["repaired_sha256"]:
                    raise ValueError("completed recovery conflicts with current writer")
                return receipt
            if current_sha not in {plan["source_sha256"], plan["repaired_sha256"]}:
                raise ValueError("overlay source SHA256 changed before apply")

            lines = _lines(raw)
            if current_sha == plan["source_sha256"]:
                checked_targets, rejected_rows, repaired = _repair_targets(
                    lines, str(plan["writer"]), requested
                )
                if checked_targets != targets:
                    raise ValueError("overlay schema diagnostic changed before apply")
                if _sha(repaired) != plan["repaired_sha256"]:
                    raise ValueError("overlay repaired SHA256 mismatch")
                _publish(evidence_fd, original_name, raw)
                for name, rejected in zip(rejected_names, rejected_rows, strict=True):
                    _publish(evidence_fd, name, rejected)
                intent_body: dict[str, Any] = {
                    "schema": RECEIPT_SCHEMA_V2 if multi else RECEIPT_SCHEMA,
                    "phase": "intent",
                    "plan_sha256": plan["plan_sha256"],
                    "source_sha256": plan["source_sha256"],
                    "repaired_sha256": plan["repaired_sha256"],
                    "actor": actor,
                    "created_at": plan["planned_at"],
                }
                if multi:
                    intent_body["targets"] = targets
                else:
                    intent_body["line_sha256"] = targets[0]["line_sha256"]
                intent = _seal(intent_body, "intent_sha256")
                _publish(evidence_fd, intent_name, _json_bytes(intent))
                _replace(
                    directory_fd,
                    str(plan["writer"]),
                    repaired,
                    str(plan["source_sha256"]),
                )
                raw = repaired
            else:
                intent_raw = _read(evidence_fd, intent_name)
                if intent_raw is None:
                    raise ValueError("repaired writer has no durable recovery intent")
                try:
                    intent = json.loads(intent_raw)
                    _verify_seal(intent, "intent_sha256")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("recovery intent is malformed") from exc
                if (
                    intent.get("plan_sha256") != plan["plan_sha256"]
                    or intent.get("source_sha256") != plan["source_sha256"]
                    or intent.get("repaired_sha256") != plan["repaired_sha256"]
                    or (multi and intent.get("targets") != targets)
                    or (not multi and intent.get("line_sha256") != targets[0]["line_sha256"])
                ):
                    raise ValueError("recovery intent does not match plan")
                original = _read(evidence_fd, original_name)
                if original is None or _sha(original) != plan["source_sha256"]:
                    raise ValueError("recovery original evidence mismatch")
                for name, target in zip(rejected_names, targets, strict=True):
                    rejected = _read(evidence_fd, name)
                    if rejected is None or _sha(rejected) != target["line_sha256"]:
                        raise ValueError("recovery rejected-line evidence mismatch")

            _validate_clean_shard(raw, str(plan["writer"]))
            receipt_body: dict[str, Any] = {
                "schema": RECEIPT_SCHEMA_V2 if multi else RECEIPT_SCHEMA,
                "disposition": "applied_and_schema_verified",
                "plan_sha256": plan["plan_sha256"],
                "writer": plan["writer"],
                "source": plan["source"],
                "source_sha256": plan["source_sha256"],
                "repaired_sha256": plan["repaired_sha256"],
                "recovery_card_id": plan["recovery_card_id"],
                "actor": actor,
                "evidence": str(evidence),
                "plan_artifact": Path(plan_path).name,
                "original_artifact": original_name,
                "intent_artifact": intent_name,
                "receipt_artifact": receipt_name,
                "verified_at": _now(),
            }
            if multi:
                receipt_body["targets"] = targets
                receipt_body["rejected_artifacts"] = rejected_names
            else:
                receipt_body["line_sha256"] = targets[0]["line_sha256"]
                receipt_body["rejected_artifact"] = rejected_names[0]
            receipt = _seal(receipt_body, "receipt_sha256")
            _publish(evidence_fd, receipt_name, _json_bytes(receipt))
            return receipt
    finally:
        os.close(evidence_fd)


def rollback_overlay_recovery(
    *, home: Path, receipt_path: Path, actor: str, writer_quiesced: bool = False
) -> dict[str, Any]:
    """Restore the exact original shard bound by one recovery receipt."""
    receipt = _load_artifact(Path(receipt_path))
    _validate_receipt(receipt)
    if receipt.get("actor") != actor:
        raise ValueError("recovery receipt identity mismatch")
    if not writer_quiesced:
        raise ValueError("overlay writers must be upgraded or quiesced before rollback")
    _validate_actor(actor)
    home = Path(home).expanduser()
    source = (home / "coordination" / "card_events" / str(receipt["writer"])).resolve(strict=True)
    if receipt.get("source") != str(source):
        raise ValueError("recovery receipt target path mismatch")
    _require_authority(home, str(receipt["recovery_card_id"]), actor)
    evidence = Path(str(receipt["evidence"]))
    evidence_fd = _evidence_directory(home, evidence)
    stem = f"overlay-{receipt['plan_sha256']}"
    intent_name = f"{stem}.rollback-intent.json"
    result_name = f"{stem}.rollback-receipt.json"
    log = CardEventLog(home)
    try:
        with log.writer_lock(str(receipt["writer"])) as directory_fd:
            raw = log._read_regular_file_bytes(directory_fd, str(receipt["writer"]))
            if raw is None:
                raise ValueError("overlay writer is absent")
            current_sha = _sha(raw)
            prior_result = _read(evidence_fd, result_name)
            if prior_result is not None:
                result = json.loads(prior_result)
                _verify_seal(result, "rollback_sha256")
                if current_sha != receipt["source_sha256"]:
                    raise ValueError("intervening writer change after rollback")
                return result
            original = _read(evidence_fd, str(receipt["original_artifact"]))
            if original is None or _sha(original) != receipt["source_sha256"]:
                raise ValueError("rollback original evidence mismatch")
            if current_sha == receipt["repaired_sha256"]:
                intent = _seal(
                    {
                        "schema": ROLLBACK_SCHEMA,
                        "phase": "intent",
                        "receipt_sha256": receipt["receipt_sha256"],
                        "expected_sha256": receipt["repaired_sha256"],
                        "restore_sha256": receipt["source_sha256"],
                        "actor": actor,
                        "created_at": receipt["verified_at"],
                    },
                    "rollback_intent_sha256",
                )
                _publish(evidence_fd, intent_name, _json_bytes(intent))
                _replace(
                    directory_fd,
                    str(receipt["writer"]),
                    original,
                    str(receipt["repaired_sha256"]),
                )
            elif current_sha == receipt["source_sha256"]:
                intent_raw = _read(evidence_fd, intent_name)
                if intent_raw is None:
                    raise ValueError("original writer has no durable rollback intent")
                try:
                    intent = json.loads(intent_raw)
                    _verify_seal(intent, "rollback_intent_sha256")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("rollback intent is malformed") from exc
                if intent.get("receipt_sha256") != receipt["receipt_sha256"]:
                    raise ValueError("rollback intent does not match receipt")
            else:
                raise ValueError("intervening writer change blocks rollback")
            restored = log._read_regular_file_bytes(directory_fd, str(receipt["writer"]))
            if restored is None or _sha(restored) != receipt["source_sha256"]:
                raise ValueError("rollback verification failed")
            result = _seal(
                {
                    "schema": ROLLBACK_SCHEMA,
                    "disposition": "rolled_back_and_hash_verified",
                    "receipt_sha256": receipt["receipt_sha256"],
                    "writer": receipt["writer"],
                    "actor": actor,
                    "restored_sha256": receipt["source_sha256"],
                    "rollback_artifact": result_name,
                    "verified_at": _now(),
                },
                "rollback_sha256",
            )
            _publish(evidence_fd, result_name, _json_bytes(result))
            return result
    finally:
        os.close(evidence_fd)
