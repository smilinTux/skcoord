"""Narrow native creation exception for one recorded operator review attempt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

ACTION = "review_replacement_authorization"


def read_artifact(home: Path, artifact: dict) -> bytes:
    """Read only exact bounded retained bytes inside the native evidence root."""
    path = Path(str(artifact.get("path", "")))
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not path.is_relative_to(home.resolve() / "evidence" / "work")
    ):
        raise ValueError("replacement evidence path invalid")
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("replacement evidence not regular")
        data = stream.read(262145)
    if (
        not data
        or len(data) > 262144
        or hashlib.sha256(data).hexdigest() != artifact.get("sha256")
    ):
        raise ValueError("replacement evidence bytes changed")
    return data


def digest(value: object) -> str:
    """Hash the canonical native authorization payload."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def events(store, card_id: str) -> list[dict]:
    """Read both native histories in a deterministic order."""
    return sorted(
        store._read_events(card_id) + store._legacy_events(card_id),
        key=lambda row: json.dumps(row, sort_keys=True),
    )


def fold_digest(card) -> str:
    """Bind the whole fold without parsing potentially invalid review links."""
    value = card.model_dump(mode="json")
    value.pop("updated_at", None)
    return digest(value)


def authorized_predecessor(store, core) -> str | None:
    """Validate a recorded exact attempt while both native card locks are held.

    This is not a caller-selectable exemption. It recognizes only one stored
    operator event, its exact unchanged source and predecessor histories, and
    the deterministic replacement identity. The application owns process and
    retained artifact qualification before appending that authorization.
    """
    from .card_store import (
        _HELD_CARD_LOCKS,
        HUMAN_CARD_CREATION_OVERRIDE_LABEL,
        _card_lock_key,
    )

    attempt = core.meta.get("review_attempt")
    if attempt is None:
        if any(
            k in core.meta
            for k in ("review_predecessor", "review_replacement_authorization")
        ):
            raise ValueError("review replacement metadata incomplete")
        return None
    source_id = core.meta.get("link_source_card", "")
    predecessor = core.meta.get("review_predecessor", "")
    if (
        not isinstance(attempt, str)
        or not re.fullmatch(r"[0-9a-f]{64}", attempt)
        or not isinstance(source_id, str)
        or not re.fullmatch(r"[0-9a-f]{8}", source_id)
        or not isinstance(predecessor, str)
        or not re.fullmatch(r"[0-9a-f]{8}", predecessor)
        or source_id == predecessor
        or HUMAN_CARD_CREATION_OVERRIDE_LABEL
        in {v.lower() for v in core.initial_labels}
        or any(
            _card_lock_key(store.home, cid) not in _HELD_CARD_LOCKS.get()
            for cid in (source_id, predecessor)
        )
    ):
        raise ValueError("review replacement binding or locks invalid")
    old = store.fold(predecessor)
    source = store.fold(source_id)
    rows = events(store, predecessor)
    authorizations = [row for row in rows if row.get("action") == ACTION]
    if len(authorizations) != 1:
        raise ValueError(
            "review replacement requires one native operator authorization"
        )
    event = authorizations[0]
    request = event.get("authorization", {})
    if not isinstance(request, dict):
        raise ValueError("review replacement authorization malformed")
    if (
        event.get("writer") != "jarvis"
        or event.get("transition_id") != attempt
        or event.get("event_id") != core.meta.get("review_replacement_authorization")
        or digest(request) != attempt
        or request.get("schema") != "skfleet.review-replacement-authorization/v1"
        or request.get("source_card") != source_id
        or request.get("predecessor") != predecessor
        or source is None
        or old is None
    ):
        raise ValueError("review replacement operator authorization invalid")
    history = [row for row in rows if row.get("action") != ACTION]
    if (
        fold_digest(source) != request.get("source_native_revision")
        or digest(events(store, source_id)) != request.get("source_events_sha256")
        or fold_digest(old) != request.get("predecessor_revision")
        or digest(history) != request.get("predecessor_events_sha256")
        or source.owner != request.get("producer")
        or not source.owner
        or source.meta.get("_claim_revision") != request.get("producer_claim")
        or old.owner != request.get("predecessor_owner")
        or not old.owner
        or old.meta.get("_claim_revision") != request.get("predecessor_claim")
        or old.owner == source.owner
        or source.meta.get("claim_conflicts")
        or old.meta.get("claim_conflicts")
        or source.archived
        or old.archived
        or source.status.value == "done"
        or old.status.value == "done"
        or "source-only" not in source.labels
        or "review" in source.labels
        or not {"review", "source-only", "do-not-claim"}.issubset(old.labels)
    ):
        raise ValueError("review replacement native custody changed")
    invalidation = request.get("invalidation", {})
    if not isinstance(invalidation, dict):
        raise ValueError("review replacement invalidation malformed")
    matching = [
        row for row in history if digest(row) == invalidation.get("event_sha256")
    ]
    if (
        len(matching) != 1
        or matching[0].get("writer") != "jarvis"
        or matching[0].get("action") != "link"
        or matching[0].get("link_key") != "operator_review_invalidation"
        or str(invalidation.get("path")) not in str(matching[0].get("link_value", ""))
        or "sha256=" + str(invalidation.get("sha256"))
        not in str(matching[0].get("link_value", ""))
    ):
        raise ValueError("review replacement invalidation missing")
    incident = json.loads(read_artifact(store.home, invalidation))
    if not isinstance(incident, dict):
        raise ValueError("review replacement invalidation receipt malformed")
    if (
        incident.get("schema") != "skfleet.independent-review-discrepancy/v1"
        or incident.get("review_card") != predecessor
        or incident.get("source_card") != source_id
        or incident.get("source_head") != request.get("source_head")
    ):
        raise ValueError("review replacement invalidation receipt changed")
    for name in ("COMPLETION-EVIDENCE.md", "REVIEW-DECISION.json"):
        read_artifact(store.home, incident["committed_artifacts"][name])
    read_artifact(store.home, request["launch_receipt"])
    expected = {
        "link_source_card": source_id,
        "link_head_revision": request.get("source_head"),
        "link_card_generation": request.get("source_generation"),
        "link_evidence_sha256": request.get("source_evidence_sha256"),
        "link_review_class": "review",
        "source_revision": request.get("source_revision"),
        "producer_identity": source.owner,
        "candidate_tree": request.get("source_tree"),
        "candidate_ref": request.get("source_ref"),
        "candidate_evidence_sha256": request.get("source_evidence_sha256"),
    }
    if any(core.meta.get(k) != v or old.meta.get(k) != v for k, v in expected.items()):
        raise ValueError("review replacement candidate metadata changed")
    for key in ("candidate_path", "base_revision"):
        if not old.meta.get(key) or core.meta.get(key) != old.meta.get(key):
            raise ValueError("review replacement source workspace changed")
    identity = "\0".join(
        (
            source_id,
            expected["link_head_revision"],
            expected["link_card_generation"],
            expected["link_evidence_sha256"],
            "review",
        )
    )
    canonical = hashlib.sha256(identity.encode()).hexdigest()[:8]
    current = hashlib.sha256(
        (identity + "\0replacement\0" + attempt).encode()
    ).hexdigest()[:8]
    if (
        predecessor != canonical
        or core.id != current
        or core.created_by != "link"
        or store._creation_class(core) != "review"
        or [x for x in core.initial_labels if x.startswith("parent-")]
        != ["parent-" + source_id]
        or "source-only" not in core.initial_labels
    ):
        raise ValueError("review replacement identity changed")
    return predecessor


@contextmanager
def creation_guard(store, core):
    """Acquire source/predecessor before the existing global creation governor."""
    from .card_store import card_mutation_lock

    if not any(
        k in core.meta
        for k in (
            "review_attempt",
            "review_predecessor",
            "review_replacement_authorization",
        )
    ):
        yield
        return
    source = core.meta.get("link_source_card", "")
    old = core.meta.get("review_predecessor", "")
    if (
        any(
            not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{8}", v)
            for v in (source, old)
        )
        or source == old
    ):
        raise ValueError("review replacement lock identity invalid")
    with card_mutation_lock(store.home, source), card_mutation_lock(store.home, old):
        authorized_predecessor(store, core)
        yield
