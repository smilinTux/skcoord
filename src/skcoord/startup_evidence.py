"""Host-independent startup evidence and pre-claim validation.

The scheduler must establish that a worker can actually start before consuming a
claim slot.  Evidence is a small signed-by-hash report: paths are deliberately
represented by portable labels and hashes, never host-local absolute paths.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

_FAILURES = {"repository", "instructions", "context", "mailbox", "malformed", "stale"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    body = {k: v for k, v in evidence.items() if k != "evidence_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest()


@dataclass(frozen=True)
class StartupCheck:
    ok: bool
    code: str
    message: str
    evidence_sha256: str = ""


def _fail(code: str, message: str) -> StartupCheck:
    return StartupCheck(False, code, message)


def validate_startup_evidence(
    evidence: Mapping[str, Any], *, card_id: str, agent: str, claim_generation: str
) -> StartupCheck:
    """Validate one report without touching CardStore or ownership.

    ``claim_generation`` is supplied by the scheduler immediately before the
    claim attempt.  A report from another generation, host, or card is stale.
    """
    if not isinstance(evidence, Mapping):
        return _fail("malformed", "pre-claim rejected: startup evidence must be an object")
    required = {"schema", "card_id", "claim_generation", "agent", "repository",
                "instructions", "context", "mailbox", "evidence_sha256"}
    if not required.issubset(evidence):
        missing = ",".join(sorted(required - set(evidence)))
        return _fail("malformed", f"pre-claim rejected: startup evidence missing {missing}")
    if evidence.get("schema") != "startup-evidence.v1":
        return _fail("malformed", "pre-claim rejected: unsupported startup evidence schema")
    if evidence["card_id"] != card_id or evidence["agent"] != agent:
        return _fail("stale", "pre-claim rejected: startup evidence belongs to another card or agent")
    if evidence["claim_generation"] != claim_generation:
        return _fail("stale", "pre-claim rejected: startup evidence is for an older claim generation")
    actual = evidence_hash(evidence)
    if evidence.get("evidence_sha256") != actual:
        return _fail("malformed", "pre-claim rejected: startup evidence hash does not match payload")
    for name, value in (("repository", evidence["repository"]),
                        ("instructions", evidence["instructions"]),
                        ("context", evidence["context"]),
                        ("mailbox", evidence["mailbox"])):
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            code = {"repository": "repository", "instructions": "instructions",
                    "context": "context", "mailbox": "mailbox"}[name]
            return _fail(code, f"pre-claim rejected: {name} startup evidence is not ready")
    return StartupCheck(True, "ok", "startup evidence validated", actual)


def prepare_startup_evidence(**fields: Any) -> dict[str, Any]:
    """Build a deterministic report using the canonical serializer."""
    report = {"schema": "startup-evidence.v1", **fields}
    report["evidence_sha256"] = evidence_hash(report)
    return json.loads(_canonical(report))
