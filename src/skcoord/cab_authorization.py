"""Verify the signed human grant stored with a CAB vote (card bada5c86).

A CAB approval counts only when the vote carries the signed CapAuth
authorization envelope it was cast under, the envelope verifies against a
PINNED approver key, and it binds exactly this change and this decision.

The pin is an operator config file, ``APPROVERS_FILE``
(``~/.config/skcapstone/cab-approvers.json``)::

    {
      "approvers": [
        {
          "identity": "chef",
          "fingerprint": "ADAD14CCAC8D6D0BF5A4209DB994E78200BF6422",
          "roles": ["owner", "approver"],
          "backend": "pgpy",
          "public_key_file": "/home/<user>/.skcapstone/capauth/identity/public.asc"
        }
      ]
    }

``public_key`` (inline armor) may be given instead of ``public_key_file``. The
key's own fingerprint must equal the pinned one. Nothing here reads
``CAPAUTH_HOME`` or the caller's CapAuth profile: who may approve is decided by
the pin, not by whoever is folding. A missing or unreadable pin means no vote
qualifies (fail closed).

What the fold checks: schema, the content-derived authorization id (equal to
the vote's ``authorization_id``), action ``itil.cab.vote.<decision>``, the
change id, role, issuer and fingerprint equal to the vote's, the issuer being
the pinned identity for that fingerprint, and the detached signature. Lifetime
and single use are enforced when the grant is redeemed (skcapstone
``itil cab vote --authorization``); a grant bound to one change and one
decision cannot approve anything else, so they are not re-checked here.

This module does not import ``skcoord.itil``; it takes votes duck-typed.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path
from typing import Any

#: Where the pinned approver keys live. Tests point this elsewhere.
APPROVERS_FILE = Path("~/.config/skcapstone/cab-approvers.json")

SCHEMA_VERSION = "atlas-authorization/v1"
HUMAN_APPROVER_ROLES = frozenset({"owner", "approver"})
#: Exactly the fields of skcapstone's ``AuthorizationEnvelope``.
ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "authorization_id",
        "issuer",
        "issuer_role",
        "issuer_fingerprint",
        "action",
        "target",
        "change_id",
        "scope",
        "issued_at",
        "expires_at",
        "nonce",
        "signature",
    }
)
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


def identity_key(identity: Any) -> str:
    """Spelling-independent comparison key for an identity: its lowercase local part.

    ``chef``, ``Chef``, ``capauth:chef@skworld.io`` and ``chef@chef.skworld.io``
    all compare equal.
    """
    key = str(identity or "").strip().casefold()
    if key.startswith("capauth:"):
        key = key[len("capauth:") :]
    return key.split("@", 1)[0].strip()


def signing_bytes(envelope: dict) -> bytes:
    """The bytes the issuer signed: every field but the id and the signature."""
    body = {k: v for k, v in envelope.items() if k not in ("authorization_id", "signature")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_id(envelope: dict) -> str:
    """The content-derived authorization id (``authz-`` + 24 hex)."""
    return "authz-" + hashlib.sha256(signing_bytes(envelope)).hexdigest()[:24]


def load_approvers(path: Path | None = None) -> dict[str, dict]:
    """Pinned approvers keyed by upper-case fingerprint; ``{}`` when absent or invalid."""
    target = Path(path or APPROVERS_FILE).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        approvers = {}
        for entry in data.get("approvers", []):
            fpr = str(entry["fingerprint"]).replace(" ", "").upper()
            armor = entry.get("public_key")
            if not armor and entry.get("public_key_file"):
                armor = Path(entry["public_key_file"]).expanduser().read_text(encoding="utf-8")
            if not fpr or not armor or not entry.get("identity"):
                continue
            approvers[fpr] = {
                "identity": str(entry["identity"]),
                "roles": frozenset(entry.get("roles") or HUMAN_APPROVER_ROLES),
                "backend": str(entry.get("backend") or "pgpy"),
                "public_key": armor,
            }
        return approvers
    except Exception:  # noqa: BLE001 - an unreadable pin pins nobody
        return {}


@functools.lru_cache(maxsize=256)
def _signature_ok(backend: str, fingerprint: str, data: bytes, signature: str, armor: str) -> bool:
    try:
        from capauth.crypto import get_backend
        from capauth.models import CryptoBackendType

        crypto = get_backend(CryptoBackendType(backend))
        if crypto.fingerprint_from_armor(armor).replace(" ", "").upper() != fingerprint:
            return False
        return bool(crypto.verify(data, signature, armor))
    except Exception:  # noqa: BLE001 - anything that cannot verify does not verify
        return False


def verify_vote_authorization(
    vote: Any,
    approvers: dict[str, dict] | None = None,
    *,
    change_id: str | None = None,
) -> bool:
    """Whether *vote* carries a stored grant that verifies against a pinned approver.

    *change_id*, when given, is the change being folded or gated, supplied by
    the caller: the vote and its envelope must both bind it, so a grant copied
    from another change's vote file does not verify.
    """
    if change_id is not None and getattr(vote, "change_id", None) != change_id:
        return False
    envelope = getattr(vote, "authorization", None)
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        return False
    if not all(isinstance(envelope[k], str) for k in ENVELOPE_FIELDS):
        return False
    decision = getattr(getattr(vote, "decision", None), "value", getattr(vote, "decision", ""))
    fingerprint = envelope["issuer_fingerprint"].replace(" ", "").upper()
    if (
        envelope["schema_version"] != SCHEMA_VERSION
        or envelope["action"] != f"itil.cab.vote.{decision}"
        or envelope["change_id"] != getattr(vote, "change_id", None)
        or envelope["issuer_role"] not in HUMAN_APPROVER_ROLES
        or envelope["issuer_role"] != getattr(vote, "subject_role", None)
        or fingerprint != str(getattr(vote, "subject_fingerprint", "")).replace(" ", "").upper()
        or envelope["authorization_id"] != content_id(envelope)
        or envelope["authorization_id"] != getattr(vote, "authorization_id", None)
        or identity_key(envelope["issuer"]) != identity_key(getattr(vote, "agent", ""))
        or not _NONCE_RE.fullmatch(envelope["nonce"])
        or not envelope["signature"]
    ):
        return False
    pinned = (load_approvers() if approvers is None else approvers).get(fingerprint)
    if pinned is None:
        return False
    if envelope["issuer_role"] not in pinned["roles"]:
        return False
    if identity_key(pinned["identity"]) != identity_key(envelope["issuer"]):
        return False
    return _signature_ok(
        pinned["backend"],
        fingerprint,
        signing_bytes(envelope),
        envelope["signature"],
        pinned["public_key"],
    )
