"""Single-writer authority for governed CardStore lifecycle mutations.

This module deliberately keeps projections out of the mutation path.  A request
is validated, serialized canonically, and appended once to CardStore; callers
may safely retry with the same request id and semantic digest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .card_store import CardStore, card_mutation_lock


ACTIONS = frozenset({"claim", "release_claim", "move", "complete", "link", "archive"})


def semantic_digest(action: str, payload: Mapping[str, Any]) -> str:
    """Hash the exact JSON request, including all fence preconditions."""
    body = {"action": action, "payload": dict(payload)}
    try:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("governed mutation payload is not canonical JSON") from exc
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MutationReceipt:
    request_id: str
    action: str
    semantic_digest: str
    event_id: str
    card_id: str

    def as_dict(self) -> dict[str, str]:
        return {"request_id": self.request_id, "action": self.action,
                "semantic_digest": self.semantic_digest, "event_id": self.event_id,
                "card_id": self.card_id}


class GovernedMutationAuthority:
    """The only lifecycle write authority for governed cards."""

    def __init__(self, home: Path):
        self.store = CardStore(Path(home).expanduser())

    def mutate(self, card_id: str, action: str, *, request_id: str,
               expected_revision: str | None = None, **payload: Any) -> dict[str, Any]:
        if action not in ACTIONS:
            raise ValueError(f"unsupported governed action: {action}")
        if not request_id or not isinstance(request_id, str):
            raise ValueError("request_id is required")
        # Never allow callers to smuggle an authority-owned field into the
        # serialized event.  This keeps retries and receipts unambiguous.
        if any(key in {"request_id", "semantic_digest", "event_id", "transition_id"}
               for key in payload):
            raise ValueError("request identity fields are authority-owned")
        if expected_revision is not None:
            payload = dict(payload)
            payload["expected_claim_revision"] = expected_revision
        digest = semantic_digest(action, payload)
        with card_mutation_lock(self.store.home, card_id):
            # Recovery is based on the durable event, not a projection or response.
            events = self.store._read_events(card_id)
            matches = [e for e in events if e.get("request_id") == request_id]
            if matches:
                if any(e.get("semantic_digest") != digest or e.get("action") != action for e in matches):
                    raise ValueError("request_id is already bound to a different mutation")
                return matches[0]
            card = self.store.fold(card_id)
            if card is None:
                raise ValueError(f"unknown governed card: {card_id}")
            if expected_revision is not None:
                actual = card.meta.get("_claim_revision")
                if actual != expected_revision:
                    raise ValueError("stale claim revision")
            if action == "archive":
                # Archive is terminal and must prove the exact completion that
                # produced it.  Links or a DONE projection are not evidence.
                completion_id = payload.get("completion_event_id")
                if card.owner is not None or card.status.value != "done" or not completion_id:
                    raise ValueError("archive requires an unowned completed card")
                events = self.store._read_events(card_id)
                completion = [e for e in events
                              if e.get("action") == "complete"
                              and e.get("event_id") == completion_id]
                if len(completion) != 1:
                    raise ValueError("archive requires the expected completion event")
            event = self.store.append_event(card_id, action, "governed-authority",
                                            request_id=request_id, semantic_digest=digest,
                                            **payload)
            return event

    def claim(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "claim", request_id=request_id, **kw)

    def release(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "release_claim", request_id=request_id, **kw)

    def move(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "move", request_id=request_id, **kw)

    def complete(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "complete", request_id=request_id, **kw)

    def link(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "link", request_id=request_id, **kw)

    def archive(self, card_id: str, request_id: str, **kw: Any) -> dict[str, Any]:
        return self.mutate(card_id, "archive", request_id=request_id, **kw)
