"""Real signed CAB grants for tests (card bada5c86).

One throwaway Ed25519 key per test session stands in for a pinned human
approver ("chef"). ``conftest.py`` pins it for every test, so no test ever reads
the operator's real pin file.
"""

from __future__ import annotations

import functools
import json
import secrets
from datetime import datetime, timedelta, timezone

from skcoord.cab_authorization import content_id, signing_bytes

PASSPHRASE = "test-only"


@functools.lru_cache(maxsize=None)
def key(name: str = "chef"):
    from capauth.crypto import get_backend
    from capauth.models import Algorithm

    return get_backend().generate_keypair(
        name, f"{name}@example.invalid", PASSPHRASE, algorithm=Algorithm.ED25519
    )


def pin_file(path, identities=("chef",), roles=("owner", "approver")) -> None:
    approvers = [
        {
            "identity": name,
            "fingerprint": key(name).fingerprint,
            "roles": list(roles),
            "backend": "pgpy",
            "public_key": key(name).public_armor,
        }
        for name in identities
    ]
    path.write_text(json.dumps({"approvers": approvers}), encoding="utf-8")


def envelope(change_id, issuer="chef", role="owner", decision="approved", signer=None):
    from capauth.crypto import get_backend

    bundle = key(signer or issuer)
    now = datetime.now(timezone.utc)
    env = {
        "schema_version": "atlas-authorization/v1",
        "authorization_id": "pending",
        "issuer": issuer,
        "issuer_role": role,
        "issuer_fingerprint": bundle.fingerprint,
        "action": f"itil.cab.vote.{decision}",
        "target": "test-target",
        "change_id": change_id,
        "scope": "test-scope",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "nonce": secrets.token_urlsafe(24),
        "signature": "",
    }
    env["authorization_id"] = content_id(env)
    env["signature"] = get_backend().sign(signing_bytes(env), bundle.private_armor, PASSPHRASE)
    return env


def signed_vote(change_id, subject="chef", role="owner", decision="approved", **kw):
    """Keyword arguments for ``submit_cab_vote`` carrying a verifying grant."""
    env = envelope(change_id, issuer=subject, role=role, decision=decision, **kw)
    return dict(
        agent=subject,
        decision=decision,
        subject=subject,
        subject_role=role,
        subject_fingerprint=env["issuer_fingerprint"],
        authorization_id=env["authorization_id"],
        authorization=env,
    )
