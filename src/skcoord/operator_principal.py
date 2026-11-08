"""Immutable operator principal provenance and reviewer independence.

SKCOORD-OPERATOR-PRINCIPAL-01

The fleet measured a class of review failures where the author and the
"reviewer" were actually one person wearing different hats: different
agent identities, different hosts, different sessions, different forge
accounts. Every actor on a card (author, recommender, reviewer,
integration actor) is resolved to a single immutable *operator principal*
-- the human operator behind the hats. When the author's principal and the
reviewer's principal are the same person, the review can never be
independent, so assignment, independent review, and merge eligibility must
fail closed.

Design rails this module follows:

* Provenance is appended as separate evidence events (``operator_principal``
  links) next to the structural CardStore events; it is never inferred from
  lifecycle state or links alone.
* The principal record is immutable once written: one record per actor per
  card, keyed by role. Re-appending the same key returns the original record
  instead of duplicating it.
* Records carry only identity facts (principal id, name, subject, issuer).
  Credentials (keys, tokens, fingerprints) and personal secrets never enter
  a record.
* Backwards compatibility is explicit and conservative: cards without
  principal records are NOT retroactively blocked. Independence checks apply
  only once at least one principal record exists for the card; a check with
  missing records fails closed for the role being decided (unknown principal
  cannot prove independence).

The alias table maps agent/forge identities to operator principals, e.g.
Lumina and Mero both act for one operator. Names are case-insensitive;
forge account aliases (``forge:<account>``) are normalised the same way.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .card_store import CardStore

__all__ = [
    "INDEPENDENCE_GATE",
    "OperatorAliasTable",
    "apply_operator_independence_gate",
    "append_operator_principal",
    "load_operator_principals",
    "merge_eligibility",
    "same_operator",
]

INDEPENDENCE_GATE = "operator-principal"

PRINCIPAL_EVIDENCE_KEY = "operator_principal"
ROLE_AUTHOR = "author"
ROLE_RECOMMENDER = "recommender"
ROLE_REVIEWER = "reviewer"
ROLE_INTEGRATION = "integration"
ROLES = (ROLE_AUTHOR, ROLE_RECOMMENDER, ROLE_REVIEWER, ROLE_INTEGRATION)

# Conservative defaults: the well-known agent identities of this estate.
# Extend via the alias table instead of editing this table.
DEFAULT_ALIAS_TABLE = {
    "chef": ["chef", "chefboy", "chefboyrdave21", "forge:chef", "forge:chefboyrdave21"],
    "jarvis": ["jarvis", "forge:jarvis"],
    "lumina": [
        "lumina",
        "Lumina",
        "capauth:lumina@skworld.io",
        "lumina@chef.skworld.io",
        "forge:lumina",
    ],
    "mero": ["mero", "Mero", "forge:mero"],
}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _normalise_alias(value: str) -> str:
    value = str(value or "").strip()
    lowered = value.lower()
    if lowered.startswith("capauth:"):
        lowered = lowered.split("@", 1)[0]
    return lowered


class OperatorAliasTable:
    """Maps many identities (agents, forge accounts, capauth subjects) to the
    operator principal they all act for.

    The table is a plain mapping from principal display name to the set of
    identities that resolve to it, with case-insensitive lookup. Unknown
    identities resolve to their own normalised form, so a principal record
    written by a new identity does not collide with a named agent identity
    unless the table explicitly unifies them.
    """

    def __init__(self, aliases: dict[str, list[str]]) -> None:
        self._by_identity: dict[str, str] = {}
        for principal, identity_list in aliases.items():
            for identity in identity_list:
                self._by_identity[_normalise_alias(identity)] = principal
        self._principals = set(aliases.keys())

    def resolve(self, identity: str) -> str:
        """Resolve one identity to an operator principal name."""
        key = _normalise_alias(identity)
        return self._by_identity.get(key, key)

    def add_alias(self, principal: str, identity: str) -> None:
        """Record that ``identity`` acts for ``principal`` (mutates this table)."""
        self._by_identity[_normalise_alias(identity)] = principal
        self._principals.add(principal)

    def principals(self) -> list[str]:
        return sorted(self._principals)


def _default_table() -> OperatorAliasTable:
    return OperatorAliasTable(DEFAULT_ALIAS_TABLE)


def load_operator_principals(store: CardStore, card_id: str) -> dict[str, dict]:
    """Return the stored principal records for one card, keyed by role.

    Reads the separate evidence store (``coordination/card_events/*.jsonl``)
    for ``operator_principal`` link events. Records are immutable: the first
    record per role wins; later duplicates are ignored, and the returned
    mapping is stable and free of credential material.
    """
    records: dict[str, dict] = {}
    evidence_dir = store.home / "coordination" / "card_events"
    if not evidence_dir.is_dir():
        return records
    for path in sorted(evidence_dir.glob("*.jsonl")):
        for line in _json_object_lines(path):
            if line.get("action") != "link":
                continue
            if _fold_key(line.get("link_key")) != PRINCIPAL_EVIDENCE_KEY:
                continue
            card_field = line.get("card_id")
            if str(card_field) != str(card_id):
                continue
            # Records carry ``role``/``principal`` only when written through
            # ``append_operator_principal``. Events carrying only a
            # ``link_value`` payload are the conservative fallback: parse the
            # JSON payload to recover role and principal.
            role = str(line.get("role") or "")
            principal = line.get("principal")
            if (not role or not principal) and isinstance(line.get("link_value"), str):
                try:
                    payload = json.loads(line["link_value"])
                    role = str(payload.get("role") or role)
                    principal = payload.get("principal", principal)
                except json.JSONDecodeError:
                    pass
            if role not in ROLES:
                continue
            if role not in records:  # immutable: first record wins
                records[role] = line
    return records


def _json_object_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _fold_key(key: object) -> str:
    """Fold an evidence link key the way lifecycle_reassessment does."""
    k = str(key or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)
    k = re.sub(r"__+", "_", k).strip("_")
    return k


def append_operator_principal(
    store: CardStore,
    card_id: str,
    role: str,
    actor_identity: str = "",
    *,
    actor_name: str = "",
    subject: str = "",
    alias_table: Optional[OperatorAliasTable] = None,
    transition_id: str = "",
) -> dict:
    """Append (or idempotently return) the immutable principal record for one actor.

    The record is written as a ``link`` event into the SEPARATE evidence store
    (``coordination/card_events/<writer>.jsonl``) rather than the structural
    CardStore log, so the provenance is joined with - not inferred from - the
    structural events. The record carries only identity facts (the resolved
    operator principal, the acting identity, an optional subject). No
    credential or personal secret is stored.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    table = alias_table or _default_table()
    principal = table.resolve(actor_identity)
    payload: dict[str, Any] = {
        "role": role,
        "principal": principal,
        "actor": actor_identity,
        "actor_name": actor_name,
    }
    if subject:
        payload["subject"] = subject
    link_value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    transition = transition_id or f"operator-principal-{card_id}-{role}"
    return _append_evidence_link_event(
        store,
        card_id,
        PRINCIPAL_EVIDENCE_KEY,
        link_value,
        writer="operator-principal",
        role=role,
        principal=principal,
        transition_id=transition,
    )


def _append_evidence_link_event(
    store: CardStore,
    card_id: str,
    link_key: str,
    link_value: str,
    *,
    writer: str = "operator-principal",
    transition_id: str = "",
    **extra: Any,
) -> dict:
    """Append one link event to the separate evidence store.

    Writes to ``coordination/card_events/<writer>.jsonl`` (the evidence
    store that ``load_operator_principals`` reads). The event carries
    ``card_id`` explicitly because the evidence store is a flat log; without
    it, the reader cannot attribute the event to a card.
    """
    import fcntl
    import uuid

    home = store.home
    events_dir = home / "coordination" / "card_events"
    events_dir.mkdir(parents=True, exist_ok=True)
    from .card_store import _HOSTNAME
    path = events_dir / f"{writer}@{_HOSTNAME}.jsonl"
    event = {
        "event_id": uuid.uuid4().hex,
        "card_id": str(card_id),
        "ts": _now_iso(),
        "writer": writer,
        "seq": 0,
        "action": "link",
        "link_key": link_key,
        "link_value": link_value,
    }
    event["transition_id"] = transition_id if transition_id else f"operator-principal-{card_id}"
    event.update(extra)
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            seq = sum(1 for _ in fh)
            fh.seek(0)
            for line in fh:
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("transition_id") == event["transition_id"]:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    return existing
            event["seq"] = seq
            fh.seek(0, os.SEEK_END)
            fh.write(json.dumps(event) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            return event
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
def same_operator(
    record_a: dict, record_b: dict, *, alias_table: Optional[OperatorAliasTable] = None
) -> bool:
    """True when two actor records resolve to the same operator principal.

    A record's principal may live in the event's top-level ``principal``
    field OR inside the ``link_value`` JSON payload (the conservative
    fallback for evidence written by other tools). A record missing a
    principal in both places cannot prove independence, so it is reported
    absent (empty), and two absent principals are considered the same
    (fail-closed for the caller's decision).
    """
    principal_a = _record_principal(record_a)
    principal_b = _record_principal(record_b)
    if not principal_a or not principal_b:
        # Fail closed: two absent principals are treated as the same operator
        # (an unknown principal cannot prove independence), so the gate blocks.
        return not (bool(principal_a) != bool(principal_b))
    table = alias_table or _default_table()
    return table.resolve(principal_a) == table.resolve(principal_b)


def _record_principal(record: dict) -> str:
    principal = record.get("principal")
    if isinstance(principal, str) and principal:
        return principal
    value = record.get("link_value")
    if isinstance(value, str):
        try:
            payload = json.loads(value)
            p = payload.get("principal")
            if isinstance(p, str) and p:
                return p
        except json.JSONDecodeError:
            pass
    return ""


def merge_eligibility(
    card_id: str,
    records: dict[str, dict],
    *,
    alias_table: Optional[OperatorAliasTable] = None,
) -> dict[str, Any]:
    """Fail-closed merge eligibility for one card.

    * No principal records at all: conservatively compatible -- the card has
      not adopted the gate, so it remains merge-eligible (explicit legacy
      behaviour).
    * Author and reviewer both recorded and resolving to the same operator:
      merge-ineligible with reason ``same_operator``.
    * A gate-relevant role is missing while other roles are recorded:
      fail closed with reason ``missing_role`` (unknown principal cannot
      prove independence).
    * Different operators: eligible with reason ``independent_operators``.
    """
    table = alias_table or _default_table()
    present = set(records.keys())
    if not present:
        return {"eligible": True, "reason": "no_principal_records", "gate": INDEPENDENCE_GATE}
    author = records.get(ROLE_AUTHOR)
    reviewer = records.get(ROLE_REVIEWER)
    if author is None or reviewer is None:
        missing = [role for role in (ROLE_AUTHOR, ROLE_REVIEWER) if records.get(role) is None]
        return {
            "eligible": False,
            "reason": "missing_role",
            "missing_roles": missing,
            "gate": INDEPENDENCE_GATE,
        }
    principal_a = _record_principal(author)
    principal_b = _record_principal(reviewer)
    if not principal_a or not principal_b:
        # Fail closed: once the gate is adopted, a missing principal cannot
        # prove independence.
        return {"eligible": False, "reason": "missing_principal", "gate": INDEPENDENCE_GATE}
    if table.resolve(principal_a) == table.resolve(principal_b):
        return {"eligible": False, "reason": "same_operator", "gate": INDEPENDENCE_GATE}
    return {"eligible": True, "reason": "independent_operators", "gate": INDEPENDENCE_GATE}


def apply_operator_independence_gate(
    store: CardStore, card_id: str, *, alias_table: Optional[OperatorAliasTable] = None
) -> dict[str, Any]:
    """Run the full gate: load records, evaluate merge eligibility.

    Returns a JSON-serialisable dict with ``eligible``, ``reason`` and the
    role records, so the caller can join it with a separate evidence event
    (``operator_independence``) next to the structural events.
    """
    table = alias_table or _default_table()
    records = load_operator_principals(store, card_id)
    decision = merge_eligibility(card_id, records, alias_table=table)
    decision["card_id"] = card_id
    decision["roles"] = sorted(records.keys())
    return decision



